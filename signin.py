"""WorkBuddy 每日签到自动领取脚本。

作者：88lin
仓库：https://github.com/88lin/workbuddy-auto-signin
协议：MIT

签到接口系从桌面端逆向所得，服务端改一版就可能失效——修复都会推到上面这个仓库。
顺手点个 ⭐ Star，等哪天连签莫名其妙断了，你能一秒把它找回来。

读取本机 WorkBuddy 桌面端的登录会话，调用其签到接口自动领取每日积分：
  POST {endpoint}/v2/billing/meter/checkin-activity-status  查询签到状态
  POST {endpoint}/v2/billing/meter/daily-checkin            领取今日积分

响应契约：
  - 领取成功 : 含 credit 字段，如 {"credit": 100}
  - 今日已签 : null 或 HTTP 400 + {"code":10001,"msg":"今天已签到，请明天再来"}
               幂等，两种形态都按"已签"处理，不计失败
  - 业务错误 : {"code": ..., "msg": ...}
  - 登录失效 : HTTP 401/403，需重新登录桌面端

凭据文件由桌面端登录后自动写入；脚本按平台自动探测，或用环境变量
WORKBUDDY_AUTH_FILE 指定。任何模式下都不会打印令牌，可安全分享。

用法：
  python signin.py auto           # 每日自动化：签到 + 成长中心（礼物/任务/补登/兑换/抽奖/Buddy）
  python signin.py silent         # 同 auto，但结果写日志文件而非 stdout（配合 pythonw.exe 静默运行）
  python signin.py growth         # 仅成长中心（不签到）
  python signin.py silent-growth  # 仅成长中心 + 写日志文件（配合成长中心轮询任务）
  python signin.py status         # 仅查签到状态（调试）
  python signin.py claim          # 仅领取签到（调试，幂等）
  python signin.py all            # 查签到状态 + 领取（调试）
"""

import json
import math
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

DEFAULT_ENDPOINT = "https://copilot.tencent.com"
AUTH_BASENAME = os.path.join("CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info")

# 伪 HTTP 码：区分"没拿到响应"的两种原因
CODE_NO_NETWORK = -1   # 连不上/超时
CODE_BUDGET_OUT = -2   # 本次运行的时间预算已耗尽，主动放弃后续请求

# 计划任务跑满 ExecutionTimeLimit 会被系统直接杀掉，届时 emit 还没执行，当天日志整条
# 丢失。这里自设更小的预算，确保总能走到写日志那一步。两个任务的时限不同，上限也必须
# 分开算：签到任务 PT10M、轮询任务 PT5M，各留 60s 给解释器启动和收尾。
# 这四个常量与 install-windows.ps1 里的 ExecutionTimeLimit 一一对应，改一处就要改另一处。
DEFAULT_BUDGET_SECONDS = 420.0
MAX_BUDGET_SECONDS = 540.0          # 签到任务 PT10M = 600s - 60s
# 成长中心轮询一天要跑好几次，单次预算相应收紧：它几乎没有必须完成的任务，
# 与其让它占满 7 分钟，不如早失败、下次再试。
POLL_BUDGET_SECONDS = 120.0
POLL_MAX_BUDGET_SECONDS = 240.0     # 轮询任务 PT5M = 300s - 60s
# 每轮最多用掉几张补登卡。卡是稀缺资源（上限 4 张），而这条写路径还没被真实响应
# 验证过，一轮只花一张：猜错形状也只错一次，一天 6 轮照样能把断登补完。
MAKEUP_MAX_PER_RUN = 1
REQUEST_TIMEOUT = 30
_started_at = None
_budget_seconds = DEFAULT_BUDGET_SECONDS
_config_warning = None   # 配置非法时的告警，由 emit 统一带进输出


def _parse_budget(default=DEFAULT_BUDGET_SECONDS, maximum=MAX_BUDGET_SECONDS):
    """解析预算环境变量。非法值/越界值一律夹到安全区间——绝不能在这里抛异常。

    这段逻辑曾写在模块顶层，WORKBUDDY_BUDGET_SECONDS=abc 会让进程在 main() 的
    try/except 生效之前就崩掉，silent 模式下当天日志整条为空。

    上限按调用方传入的 maximum 算：轮询任务的 ExecutionTimeLimit 比签到任务短，
    共用一个 540s 的上限等于对轮询任务没有上限，跑穿一样会被杀在写日志之前。
    """
    raw = os.environ.get("WORKBUDDY_BUDGET_SECONDS")
    if not raw:
        return default, None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default, "WORKBUDDY_BUDGET_SECONDS=%r 不是数字，已回落 %s 秒" % (
            raw, int(default))
    if val <= 0:
        # "0" 是非空字符串，用 `or` 兜不住；且 0 会让每个请求都直接放弃，脚本永久失效
        return default, "WORKBUDDY_BUDGET_SECONDS=%s 必须为正数，已回落 %s 秒" % (
            raw, int(default))
    if val > maximum:
        # 上限同样是硬要求：预算大于任务时限就等于没有预算，黑洞式超时会把进程跑到被强杀
        return maximum, ("WORKBUDDY_BUDGET_SECONDS=%s 超过本命令上限，已夹到 %s 秒"
                         "（须小于该定时任务的 ExecutionTimeLimit）") % (
            raw, int(maximum))
    return val, None


def _start_budget(action=None):
    """启动预算时钟；配置非法时记下告警，由 emit 带进输出而不是静默回落。

    轮询类命令（silent-growth）用更短的默认值和更低的上限，用户显式设置环境变量时
    仍以环境变量为准，但同样夹在该命令自己的上限内。
    """
    global _started_at, _budget_seconds, _config_warning
    if action == "silent-growth":
        default, maximum = POLL_BUDGET_SECONDS, POLL_MAX_BUDGET_SECONDS
    else:
        default, maximum = DEFAULT_BUDGET_SECONDS, MAX_BUDGET_SECONDS
    _budget_seconds, _config_warning = _parse_budget(default, maximum)
    _started_at = time.monotonic()


def _budget_left():
    """本次运行还剩多少秒可用于网络请求。"""
    if _started_at is None:
        return _budget_seconds
    return _budget_seconds - (time.monotonic() - _started_at)


def find_auth_file():
    """按平台探测 WorkBuddy 桌面端写出的登录凭据文件，支持环境变量覆盖。

    返回 (path_or_None, looked_in) —— looked_in 始终是脚本实际检查过的路径列表，
    供 NO_AUTH 报错时显示，避免与代码实现漂移。
    """
    override = os.environ.get("WORKBUDDY_AUTH_FILE")
    if override:
        # 环境变量优先，但文件不存在时单独报告，不给无关建议
        return (override if os.path.exists(override) else None), [override]
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    candidates = [
        os.path.join(local, AUTH_BASENAME),                                  # Windows
        os.path.join(home, "Library", "Application Support", AUTH_BASENAME),  # macOS
        os.path.join(home, ".config", AUTH_BASENAME),                        # Linux
        os.path.join(home, ".workbuddy", "auth", "workbuddy-desktop.info"),  # 兜底
    ]
    for c in candidates:
        if os.path.exists(c):
            return c, candidates
    return None, candidates


def load_session(auth_file):
    with open(auth_file, "r", encoding="utf-8") as f:
        return json.load(f)


def load_session_retry(auth_file, attempts=3, delay=2.0):
    """带重试的凭据读取。

    WorkBuddy 客户端刷新 token 时会短暂独占凭据文件，恰好在那一刻读取就是
    PermissionError（实测：定时任务两次撞锁、整轮直接放弃）。锁是瞬时的，
    等两秒再试即可；重试仍失败才真正报错。
    """
    last = None
    for i in range(attempts):
        try:
            return load_session(auth_file)
        except PermissionError as e:
            last = e
            if i < attempts - 1:
                time.sleep(delay)
    raise last


def build_headers(session):
    auth = session.get("auth") or {}
    account = session.get("account") or {}
    token = auth.get("accessToken")
    uid = account.get("uid")
    if not token or not uid:
        raise ValueError("NO_SESSION: 本地未找到有效登录会话")
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/json",
        "X-User-Id": uid,
        "User-Agent": "WorkBuddy",
    }
    if account.get("enterpriseId"):
        headers["X-Enterprise-Id"] = account["enterpriseId"]
        headers["X-Tenant-Id"] = account["enterpriseId"]
    if auth.get("domain"):
        headers["X-Domain"] = auth["domain"]
    return headers


def _request(url, headers, method="GET", payload=None, timeout=REQUEST_TIMEOUT):
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"raw": raw[:500]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:500]}
    except urllib.error.URLError as e:
        return CODE_NO_NETWORK, {"error": str(e.reason)}
    except Exception as e:
        return CODE_NO_NETWORK, {"error": str(e)}


def post(url, headers, payload=None, retry=False):
    """POST 默认不重试：抽奖/领奖等写操作若在服务端处理完成后才超时，重试会重复提交。"""
    return _request_with_retry(url, headers, method="POST", payload=payload,
                               retries=1 if retry else 0)


def get(url, headers):
    return _request_with_retry(url, headers, method="GET", retries=1)


def _request_with_retry(url, headers, method="GET", payload=None, retries=1, delay=5):
    """带时间预算的请求：超时上限随剩余预算收缩，预算不足则直接放弃而不是硬等。

    返回 CODE_BUDGET_OUT 表示"没发出去，因为再发就要超出任务时限了"——调用方据此提前
    收尾，保证 emit 一定能执行到。
    """
    left = _budget_left()
    if left <= 1:
        return CODE_BUDGET_OUT, {"error": "已达本次运行时间预算，跳过剩余请求"}

    code, body = _request(url, headers, method=method, payload=payload,
                          timeout=max(1, min(REQUEST_TIMEOUT, left)))
    for _ in range(retries):
        if code != CODE_NO_NETWORK:
            break
        # 重试要占掉 delay + 一整个超时，预算不够就别开始
        if _budget_left() <= delay + REQUEST_TIMEOUT:
            break
        time.sleep(delay)
        code, body = _request(url, headers, method=method, payload=payload,
                              timeout=max(1, min(REQUEST_TIMEOUT, _budget_left())))
    return code, body


def _is_hard_failure(code):
    """是否属于"需要人关注"的失败。

    5xx 与"没拿到响应"算硬失败；4xx 绝大多数是业务规则（如派 Buddy 已达每日上限、
    活动已结束），属于每天的正常状态，若计入退出码会让计划任务天天报红。
    """
    return code >= 500 or code in (CODE_NO_NETWORK, CODE_BUDGET_OUT)


def _http_label(code):
    """把伪 HTTP 码翻译成人话；-1/-2 是脚本自定义的"没拿到响应"标记。"""
    if code == CODE_NO_NETWORK:
        return "网络不可达"
    if code == CODE_BUDGET_OUT:
        return "时间预算耗尽"
    return "HTTP %s" % code


def _is_no_chance(msg):
    """抽奖失败是否只是"没有次数"——这是常态，不是故障。

    服务端对"次数为 0"返回 400 + `insufficient lottery chance balance`，和真正的
    参数错误（`invalid request`）同为 400，只看状态码会把两者混为一谈：把常态记成
    失败，轮询就会每轮强制落盘、还会把失败数算进汇报。
    """
    m = str(msg or "").lower()
    if not m:
        return False
    if "insufficient" in m or "not enough" in m:
        return "chance" in m or "balance" in m
    return "no chance" in m


def _is_unknown_tier(code, body):
    """连登兑换是否因为"tier 这个值本身不认识"被拒——用于判断要不要换一种写法重试。

    /redeem 实测收的是天数（7/14/28），档位名会得到 400 + `unknown tier`。但这一点
    只在一台机器上验过，接口哪天要是改成只认档位名，脚本就会三档全废且看不出原因。
    这类 400 是参数校验阶段的拒绝，服务端没兑换任何东西，换个写法重试是安全的；
    `invalid request`（未解锁）这种业务拒绝不在此列，不能重试。
    """
    if code != 400:
        return False
    m = str(dig(body, "msg") or "").lower()
    return "tier" in m and ("unknown" in m or "invalid" in m or "unsupported" in m)


def dig(obj, key):
    """在可能被 data/result 包裹的响应里找字段，兼容信封结构。"""
    if isinstance(obj, dict):
        if key in obj and obj[key] is not None:
            return obj[key]
        for k in ("data", "result", "resp", "response"):
            if k in obj and isinstance(obj[k], dict):
                r = dig(obj[k], key)
                if r is not None:
                    return r
    return None


def fmt_credit(v):
    """积分显示用：能转 int 就转，否则原样返回（OverflowError 同理，见 as_int）。"""
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return v


def as_int(v, default=0):
    """把可能为字符串的数字安全地转成 int，避免与数值比较/累加时抛异常。

    OverflowError 必须一并捕获：json.loads 默认接受 Infinity，服务端返回该字面量时
    v 已经是 float('inf')，int(v) 抛的是 OverflowError 而非 ValueError。
    """
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return int(float(v))  # 兼容 "1.5"/"1e3" 这类数字串，宁可截断也不把真实数值丢成 0
    except (TypeError, ValueError, OverflowError):
        return default


def _first_int(body, key, fallback=0):
    """优先取响应里的实际数值，挖不到才用回落值。

    任务列表里的 reward_credit 只是活动配置，与服务端这次实际发放的可能不同；
    上报按响应值才不会虚报，响应里没有时才退回配置值。
    """
    if isinstance(body, dict):
        v = dig(body, key)
        if v is not None:
            return as_int(v)
    return as_int(fallback)


def _fmt_eta(arrive_at, server_now):
    """把服务端返回的 Unix 时间戳换算成"还有多久回来"。

    任一时间戳缺失/非法就返回空串——这是纯展示信息，绝不能因为它让整轮执行失败。
    json.loads 默认接受 Infinity/NaN，非有限值同样按缺失处理，否则会汇报出
    "约 inf 小时后回"这种话。
    """
    try:
        left = float(arrive_at) - float(server_now)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(left):
        return ""
    if left <= 0:
        return "，已到达待领取"
    # 先算分钟再决定用哪个量纲：直接按 left < 3600 分档会让 3599s 显示成"约 60 分钟"
    minutes = int(round(left / 60.0))
    if minutes < 60:
        return "，约 %d 分钟后回" % max(1, minutes)
    return "，约 %.1f 小时后回" % (left / 3600.0)


def _env_flag(name):
    """开关型环境变量是否为真；空串与 0/false/no/off 一律视为关。"""
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _client_token(prefix="u"):
    """活动接口（抽奖/连登兑换）要求的防重放 token。

    官方前端用 `crypto.randomUUID()` 拼成 "u-<uuid>"，服务端只做幂等去重、
    不校验格式；缺了它 /lottery/draw 直接 400（实测过）。
    """
    return "%s-%s" % (prefix, uuid.uuid4())


# 连登兑换各档奖励的官方文案（2026-09 活动版本）。兑换成功的汇报优先用
# 服务端返回的实际明细，挖不到字段时才回落到这里——活动改版时以响应为准。
_REDEEM_REWARDS = {
    "starter":   "+2 能量 +1 补登卡 +1 次抽奖",
    "advanced":  "+50 积分 +3 能量 +1 补登卡 +1 次抽奖",
    "legendary": "+150 积分 +5 能量 +1 补登卡 +1 次抽奖",
}


def _redeem_reward_desc(body, tier):
    """兑换成功的奖励描述：能从响应里挖到积分/能量就拼实际值，否则用官方文案。"""
    bits = []
    credit = as_int(dig(body, "credit"), 0)
    energy = as_int(dig(body, "energy"), 0)
    if credit:
        bits.append("+%s 积分" % fmt_credit(credit))
    if energy:
        bits.append("+%s 能量" % fmt_credit(energy))
    if bits:
        return "（%s）" % " ".join(bits)
    return "（%s）" % _REDEEM_REWARDS.get(tier, "奖励已到账")


def _dumps(out):
    """序列化汇报内容；default=str 兜住意外混入的非 JSON 类型，绝不让唯一的输出通道崩掉。"""
    try:
        return json.dumps(out, ensure_ascii=False, default=str)
    except Exception:
        return repr(out)


def emit(out, action):
    """silent 类模式写日志文件，其余模式打印到 stdout；本函数保证不抛异常。

    判定用前缀而不是等号：silent-growth 同样是无窗口跑的，pythonw 下 sys.stdout 是
    None，print 会静默成功（不抛异常），于是"打到 stdout"等于扔进黑洞——NO_AUTH、
    NO_SESSION 这类最需要人处理的结果会一条不剩地消失。

    ERROR 两条通道都走：计划任务里若把命令名写错（silent 拼成 slient 之类），
    action 不带 silent 前缀，走 stdout 就等于扔进黑洞，无窗口运行下这次失败再没有
    任何痕迹——正是本脚本想杜绝的"当天日志整条丢失"。
    """
    if _config_warning and isinstance(out, dict):
        out = dict(out, config_warning=_config_warning)
    payload = _dumps(out)
    is_error = isinstance(out, dict) and out.get("result") == "ERROR"
    if not str(action).startswith("silent"):
        try:
            print(payload)
            if not is_error:
                return
        except Exception:
            pass  # stdout 不可用（编码/管道问题）时退到日志，至少不把结果丢掉

    line = "[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), payload)
    default_log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signin.log")
    # 自定义路径不可写时回退到脚本同目录，避免无窗口运行下结果彻底丢失
    for path in (os.environ.get("WORKBUDDY_SIGNIN_LOG") or default_log, default_log):
        try:
            with open(path, "a", encoding="utf-8") as lf:
                lf.write(line)
            return
        except Exception:
            continue


def _is_already_checked_in(cbody):
    """领取接口返回是否表示"今日已签"（兼容 null 与 400+code10001）。"""
    if cbody is None:
        return True
    if isinstance(cbody, dict):
        msg = cbody.get("msg") or ""
        if cbody.get("code") == 10001 or "已签" in msg:
            return True
    return False


def _already_report(status, via=None):
    """根据状态构造"今日已签"汇报 dict。"""
    today_credit = dig(status, "today_credit") or dig(status, "daily_credit")
    streak_days = dig(status, "streak_days")
    total_credits = dig(status, "total_credits")
    is_streak_day = dig(status, "is_streak_day")
    next_streak_day = dig(status, "next_streak_day")
    inner = []
    if today_credit is not None:
        inner.append("今日 +%s" % fmt_credit(today_credit))
    if streak_days is not None:
        inner.append("连续 %s 天" % streak_days)
    if total_credits is not None:
        inner.append("累计 %s 积分" % fmt_credit(total_credits))
    prefix = via or "今日已签过"
    report = "%s（%s）" % (prefix, "，".join(inner)) if inner else prefix
    return {
        "result": "ALREADY",
        "report": report,
        "today_credit": today_credit,
        "streak_days": streak_days,
        "total_credits": total_credits,
        "is_streak_day": is_streak_day,
        "next_streak_day": next_streak_day,
    }


def run_growth(headers, endpoint):
    """成长中心自动化：领旅行礼物→派 Buddy→领任务/领取新任务→补登→连登兑换→开盲盒→能量开 Buddy→汇报。

    各子步骤单独 try，一段失败不影响其余领取；任一步遇 401/403 直接升级为 NO_SESSION。
    """
    base = endpoint + "/v2/activity/growth"
    parts = []
    credits_gained = 0
    failures = 0        # 全部失败项，仅用于汇报
    hard_failures = 0   # 其中"需要关注"的那些，只有它们影响退出码
    successes = 0

    def _check_auth(code):
        """返回 True 表示需要立即退出（登录态失效）。"""
        return code in (401, 403)

    def _note_http(code, body, label):
        """前置查询接口非 2xx 时的统一记录；返回 True 表示调用方应跳过后续处理。

        硬失败（5xx / 网络不可达 / 预算耗尽）必须计入 failures：否则整轮
        successes=0 且 failures=0 会被判成 idle，轮询既不写日志又返回 0，
        服务端故障彻底无声无息。旅行模块早就这么做了，其余各段没跟上。

        4xx 只进报告、不计失败：绝大多数是业务规则（活动未开始、接口下线），
        计入会让轮询天天强制落盘。手动跑 `growth` 仍能在报告里看到它。
        """
        nonlocal failures, hard_failures
        if 200 <= code < 300:
            return False
        reason = _http_label(code)
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("error") or body.get("msg") or "")
        # 状态码与服务端给的详情都保留：只知道 500 无法判断影响，只知道 "timed out"
        # 又看不出是网络还是服务端，轮询日志里这两者都想要
        parts.append("%s失败：%s" % (label, "%s（%s）" % (reason, detail)
                                     if detail and detail != reason else (detail or reason)))
        if _is_hard_failure(code):
            failures += 1
            hard_failures += 1
        return True

    # --- 1. Buddy 旅行：领礼物 + 派出发 ---
    try:
        scode, sbody = get(base + "/buddy/travel/status", headers)
        if scode == CODE_BUDGET_OUT:
            return 1, {"result": "TIMEOUT",
                       "report": "时间预算耗尽，成长中心跳过，下次自动重试"}
        if scode == CODE_NO_NETWORK:
            # 网络不可达就立刻收手，别把后续 5 个接口的重试+等待全跑一遍
            return 1, {"result": "NETWORK",
                       "report": "网络不可达，成长中心跳过（%s）" % (sbody.get("error") or "")}
        if _check_auth(scode):
            return 1, {"result": "NO_SESSION",
                       "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
        travel = dig(sbody, "state") if (200 <= scode < 300) else None
        # 服务端明确给出"今日旅行名额已用完"。读它而不是等 depart 报错，
        # 轮询场景下差别很大：后者会让每一轮都白撞一次墙。
        daily_limit = bool(dig(sbody, "daily_limit_reached")) if (200 <= scode < 300) else False
        _note_http(scode, sbody, "查旅行状态")
        claimed_travel = False
        if travel == "arrived":
            record_id = dig(sbody, "record_id")
            ccode, cbody = post(base + "/buddy/travel/claim", headers, {"record_id": record_id})
            if _check_auth(ccode):
                return 1, {"result": "NO_SESSION",
                           "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
            if 200 <= ccode < 300 and dig(cbody, "reward_credit") is not None:
                got = as_int(dig(cbody, "reward_credit"))
                credits_gained += got
                parts.append("领旅行礼物 +%s 积分" % fmt_credit(got))
                successes += 1
                claimed_travel = True
            else:
                # 领失败：带出业务 msg，不再说"HTTP 200"；也不派 Buddy 出发，避免覆盖未领取的奖励
                msg = (dig(cbody, "msg") or "") if isinstance(cbody, dict) else ""
                parts.append("领旅行礼物失败：%s" % (msg or "HTTP %s" % ccode))
                failures += 1
                hard_failures += _is_hard_failure(ccode)
            if claimed_travel:
                travel = "idle"  # 只有领取成功后才视为 idle，允许派出发
        if travel == "idle" and daily_limit:
            # 今日名额已用完：直接收手，不碰 config/depart，省掉两个请求和一条必然的失败
            parts.append("今日旅行名额已用完")
        elif travel == "idle":
            ccode, cbody = get(base + "/buddy/travel/config", headers)
            if _check_auth(ccode):
                return 1, {"result": "NO_SESSION",
                           "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
            locs = dig(cbody, "locations") if (200 <= ccode < 300) else None
            if locs and isinstance(locs[0], dict):
                loc = locs[0]
                dcode, dbody = post(base + "/buddy/travel/depart", headers,
                                    {"location_id": loc.get("id")})
                if _check_auth(dcode):
                    return 1, {"result": "NO_SESSION",
                               "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
                if 200 <= dcode < 300:
                    loc_name = (dig(dbody, "location") or {}).get("name", "?")
                    dur = dig(dbody, "duration_hours") or (dig(dbody, "location") or {}).get("duration_hours", "?")
                    parts.append("派 Buddy 去%s（%s 小时后回）" % (loc_name, dur))
                    successes += 1
                else:
                    msg = dig(dbody, "msg") or ""
                    parts.append("派 Buddy 失败：%s" % (msg or "HTTP %s" % dcode))
                    failures += 1
                    hard_failures += _is_hard_failure(dcode)
        elif travel == "traveling":
            loc_name = (dig(sbody, "location") or {}).get("name", "?")
            # arrive_at / server_now 是服务端时间戳，比本地时钟可靠
            parts.append("Buddy 旅行中（%s%s）" % (
                loc_name, _fmt_eta(dig(sbody, "arrive_at"), dig(sbody, "server_now"))))
    except Exception as e:
        parts.append("旅行模块异常（%s: %s）" % (type(e).__name__, e))
        failures += 1
        hard_failures += 1

    # --- 2. 任务领奖（放在抽奖前：任务送的抽奖机会/能量，后面马上能用上）---
    if _budget_left() <= 0:
        parts.append("时间预算耗尽，任务领奖跳过")
    else:
        try:
            tcode, tbody = get(base + "/tasks", headers)
            if _check_auth(tcode):
                return 1, {"result": "NO_SESSION",
                           "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
            if not _note_http(tcode, tbody, "查任务列表"):
                tasks = dig(tbody, "tasks") or []
                for t in tasks:
                    if _budget_left() <= 0:
                        parts.append("时间预算耗尽，剩余任务下次再领")
                        break
                    try:
                        # accept_status 三态：None/"" 未领取 · "accepted" 已领取进行中 · "claimed" 已领奖。
                        # 官方规则：进度从"领取任务"那一刻才开始计，领取前的使用行为不算。
                        # 所以除了领奖，还得替用户把新出现的任务领下来，否则它永远完不成。
                        status = t.get("accept_status")
                        if status == "claimed" or t.get("locked"):
                            continue
                        prog = t.get("progress") or {}
                        target = as_int(prog.get("target"), 1) or 1  # target 为 0/缺失时按 1 处理，避免误判已完成
                        done = as_int(prog.get("current")) >= target
                        if not done and status:
                            continue  # 已领取、还没做完——等用户完成，别重复领取
                        # claiming=True 才是"领奖"：已领取过 + 已完成。未领取的任务即使
                        # 进度显示已达标也只当"领取"，因为这一 POST 大概率只是接单而非发奖，
                        # 按领奖上报会把没到账的积分算进 credits_gained；下一轮再正常领奖。
                        claiming = bool(done and status)
                        # 不再拿 has_reward 做前置条件：实测 16 个任务全部为 true，连 14 个
                        # 已 claimed 的也是——它是"该任务设有奖励"的静态属性，不是"有待领取的
                        # 奖励"。用它拦截只会把 has_reward=false 却真有奖励的任务永久漏掉。
                        acode, abody = post(base + "/tasks/accept", headers,
                                            {"task_code": t.get("task_code")})
                        if _check_auth(acode):
                            return 1, {"result": "NO_SESSION",
                                       "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
                        title = t.get("title", t.get("task_code"))
                        if 200 <= acode < 300:
                            if claiming:
                                # 优先用服务端实际发放的数值；列表里的 reward_credit
                                # 只是活动配置，改版时会和实发对不上（旅行礼物、连登兑换
                                # 都已按响应值上报，这里保持一致）。挖不到才回落到列表值。
                                rc = _first_int(abody, "credit", t.get("reward_credit"))
                                re_ = _first_int(abody, "energy", t.get("reward_energy"))
                                credits_gained += rc
                                parts.append("领任务奖「%s」+credit%s+energy%s" % (title, rc, re_))
                            else:
                                parts.append("领取任务「%s」（进度开始计）" % title)
                            successes += 1
                        else:
                            # 失败必须记：这个 POST 现在每轮都会为未领取的任务发一次，
                            # 静默吞掉就等于接口坏了也没人知道（轮询下空跑是不写日志的）
                            msg = dig(abody, "msg") or ""
                            parts.append("%s「%s」失败：%s" % (
                                "领任务奖" if claiming else "领取任务",
                                title, msg or "HTTP %s" % acode))
                            failures += 1
                            hard_failures += _is_hard_failure(acode)
                    except Exception as e:
                        parts.append("任务「%s」异常（%s: %s）" % (
                            t.get("task_code", "?"), type(e).__name__, e))
                        failures += 1
                        hard_failures += 1
        except Exception as e:
            parts.append("任务模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 3. 补登卡：断登自动补一张，保住连登 ---
    # 官方规则：补登卡上限 4 张、仅可补救当月断登；/streak 的 makeup_dates
    # 是服务端算好的可补日期。卡攒着不花，超上限后新卡也拿不到，断登优先补。
    # 放在连登兑换之前：补登会改变连登天数，先补，兑换才能拿到最新解锁状态。
    # 注意：实测时 makeup_dates 一直是 []，这条写路径没有被真实响应验证过，
    # 所以每轮最多补一张（见 MAKEUP_MAX_PER_RUN），万一形状猜错也只错一次。
    streak_body = None    # 复用给第 7 段的展示值，避免同一轮打两次 /streak
    streak_stale = False  # 补登成功会改变连签天数，此时必须重新取
    if _budget_left() <= 0:
        parts.append("时间预算耗尽，补登跳过")
    else:
        try:
            mcode, mbody = get(base + "/streak", headers)
            if _check_auth(mcode):
                return 1, {"result": "NO_SESSION",
                           "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
            if not _note_http(mcode, mbody, "查连登状态"):
                streak_body = mbody
                # 余额兼容两种形状：{"makeup_cards":{"balance":2}} 与 {"makeup_cards":2}。
                # 只认前者的话，接口是后者时整个补登会一声不响地永不执行。
                cards_obj = dig(mbody, "makeup_cards")
                cards = as_int(cards_obj.get("balance")) if isinstance(cards_obj, dict) \
                    else as_int(cards_obj)
                # 实测 makeup_dates 在 streak 对象内部（不在顶层），当前值为 []；
                # dig 只做顶层查找，这里手动下钻，两处都兜住以防接口调整。
                streak_obj = dig(mbody, "streak") or {}
                dates = (streak_obj.get("makeup_dates") if isinstance(streak_obj, dict) else None) \
                    or dig(mbody, "makeup_dates") or []
                if cards > 0 and isinstance(dates, list) and dates:
                    for d in dates[:min(cards, MAKEUP_MAX_PER_RUN)]:
                        if _budget_left() <= 0:
                            parts.append("时间预算耗尽，剩余补登下次再做")
                            break
                        ucode, ubody = post(base + "/makeup-cards/use", headers,
                                            {"target_date": d, "client_token": _client_token()})
                        if _check_auth(ucode):
                            return 1, {"result": "NO_SESSION",
                                       "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
                        if 200 <= ucode < 300:
                            cards -= 1
                            streak_stale = True
                            # 优先报服务端给的余额，本地递减只是接口没给时的兜底
                            left_obj = dig(ubody, "makeup_cards")
                            left_cards = as_int(left_obj.get("balance"), cards) \
                                if isinstance(left_obj, dict) else as_int(left_obj, cards)
                            parts.append("补登 %s（剩 %s 张卡）" % (d, left_cards))
                            successes += 1
                        else:
                            msg = dig(ubody, "msg") or ""
                            parts.append("补登 %s 失败：%s" % (d, msg or "HTTP %s" % ucode))
                            failures += 1
                            hard_failures += _is_hard_failure(ucode)
                    if len(dates) > MAKEUP_MAX_PER_RUN and cards > 0:
                        parts.append("另有 %s 天可补、剩 %s 张卡，下轮继续" % (
                            len(dates) - MAKEUP_MAX_PER_RUN, cards))
        except Exception as e:
            parts.append("补登模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 4. 连登奖励兑换（入门/进阶/巅峰三档，附积分/能量/补登卡/抽奖机会）---
    # 入门 7 天、进阶 14 天、巅峰 28 天解锁。summary 只报 claimed/locked 两种
    # 已见状态；"非 claimed 且非 locked"即视为可兑换去尝试，被服务端拒绝时按
    # 普通业务失败处理（不算硬失败）。
    if _budget_left() <= 0:
        parts.append("时间预算耗尽，连登兑换跳过")
    else:
        try:
            rcode, rbody = get(base + "/redeem/summary", headers)
            if _check_auth(rcode):
                return 1, {"result": "NO_SESSION",
                           "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
            if not _note_http(rcode, rbody, "查连登兑换"):
                # tier 传「天数」：实测传 "starter" 这类档位名直接 400 unknown tier，
                # 传 7/14/28 才被识别（未解锁时回 invalid request，那才是业务拒绝）。
                # 档位名只用于读 *_status 字段和展示文案。
                # 这一点只在一台机器上验过，接口若改成只认档位名，下面有兜底重试。
                for tier, label, days in (("starter", "入门", 7),
                                          ("advanced", "进阶", 14),
                                          ("legendary", "巅峰", 28)):
                    if _budget_left() <= 0:
                        parts.append("时间预算耗尽，剩余连登兑换下次再领")
                        break
                    status = dig(rbody, tier + "_status")
                    # 字段缺失（None）同样跳过：接口改版时不该让脚本对三档无脑 POST。
                    # 注意别在这里再加 *_count 之类的"双保险"：实测响应里
                    # starter_count=1 与 total_consumed=0 并存，count 到底是
                    # "已兑换次数"还是"可兑换次数"并不确定，猜错就会把整段静默关掉。
                    if not status or status in ("claimed", "locked"):
                        continue
                    c2code, c2body = post(base + "/redeem", headers,
                                          {"tier": days, "client_token": _client_token()})
                    # 天数被判为未知档位时退回档位名再试一次：这类 400 是参数校验阶段
                    # 的拒绝，服务端没兑换任何东西，重试不会重复领取
                    if _is_unknown_tier(c2code, c2body):
                        c2code, c2body = post(base + "/redeem", headers,
                                              {"tier": tier, "client_token": _client_token()})
                    if _check_auth(c2code):
                        return 1, {"result": "NO_SESSION",
                                   "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
                    if 200 <= c2code < 300:
                        credits_gained += as_int(dig(c2body, "credit"))
                        parts.append("连登兑换「%s」%s" % (label, _redeem_reward_desc(c2body, tier)))
                        successes += 1
                    else:
                        msg = dig(c2body, "msg") or ""
                        parts.append("连登兑换「%s」失败：%s" % (label, msg or "HTTP %s" % c2code))
                        failures += 1
                        hard_failures += _is_hard_failure(c2code)
        except Exception as e:
            parts.append("连登兑换模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 5. 盲盒/抽奖（draw 必须带 client_token，缺了会 400）---
    if _budget_left() <= 0:
        parts.append("时间预算耗尽，盲盒跳过")
    else:
        try:
            lcode, lbody = get(base + "/lottery/chances", headers)
            if _check_auth(lcode):
                return 1, {"result": "NO_SESSION",
                           "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
            chances = 0 if _note_http(lcode, lbody, "查抽奖机会") else as_int(dig(lbody, "balance"))
            if chances > 0:
                dcode, dbody = post(base + "/lottery/draw", headers,
                                    {"client_token": _client_token()})
                if _check_auth(dcode):
                    return 1, {"result": "NO_SESSION",
                               "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
                if 200 <= dcode < 300:
                    prize = dig(dbody, "prize_name") or dig(dbody, "prize") or "未知"
                    if not isinstance(prize, str):
                        # prize 可能是对象/数字；直接 += 会抛 TypeError，把一次已经中了的
                        # 抽奖变成"模块异常"，奖品名和下面这句提醒双双丢失
                        prize = str(prize)
                    # 奖池含实物周边（冰箱贴/胸针/杯子），中奖后要用户自己去填收件信息，
                    # 脚本代填不了也绝不该代填——但必须提醒，否则奖品会卡在未填地址状态。
                    if dig(dbody, "need_address") or dig(dbody, "require_address"):
                        prize += "（实物奖，需到成长中心填写收件信息）"
                    parts.append("开盲盒获得：%s" % prize)
                    successes += 1
                    # 一轮只开一次：这条写路径不可逆，剩下的机会留给下一轮更稳妥
                    if chances > 1:
                        parts.append("还剩 %s 次抽奖机会，下轮继续" % (chances - 1))
                else:
                    msg = dig(dbody, "msg") or ""
                    if _is_no_chance(msg):
                        # 次数为 0 是常态（次数来自连登兑换），不是故障：计入失败会让
                        # 轮询每轮强制落盘，还会把常态算进失败统计
                        parts.append("开盲盒：%s" % (msg or "无抽奖机会"))
                    else:
                        parts.append("开盲盒失败：%s" % (msg or "HTTP %s" % dcode))
                        failures += 1
                        hard_failures += _is_hard_failure(dcode)
        except Exception as e:
            parts.append("盲盒模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 6. Buddy 盲盒（能量攒够 cost_per_open 就开；能量没有其它消耗出口）---
    if _budget_left() <= 0:
        parts.append("时间预算耗尽，Buddy 盲盒跳过")
    else:
        try:
            qcode, qbody = get(base + "/buddy/quota", headers)
            if _check_auth(qcode):
                return 1, {"result": "NO_SESSION",
                           "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
            if not _note_http(qcode, qbody, "查 Buddy 能量"):
                affordable = as_int(dig(qbody, "affordable"))
                max_open = as_int(dig(qbody, "max_open_count"), 1) or 1
                if affordable > 0:
                    count = min(affordable, max_open)
                    ocode, obody = post(base + "/buddy/open", headers,
                                        {"count": count, "client_token": _client_token()})
                    if _check_auth(ocode):
                        return 1, {"result": "NO_SESSION",
                                   "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
                    if 200 <= ocode < 300:
                        name = dig(obody, "buddy") or dig(obody, "name") or dig(obody, "buddies")
                        if not isinstance(name, str):
                            name = "新 Buddy"
                        parts.append("开 Buddy 盲盒 ×%s（%s）" % (count, name))
                        successes += 1
                    else:
                        msg = dig(obody, "msg") or ""
                        parts.append("开 Buddy 盲盒失败：%s" % (msg or "HTTP %s" % ocode))
                        failures += 1
                        hard_failures += _is_hard_failure(ocode)
        except Exception as e:
            parts.append("Buddy 盲盒模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 7. 能量 & 连签状态（纯展示值，预算不够就直接不取，不计失败）---
    energy = None
    streak_days = None
    if _budget_left() > 0:
        try:
            ecode, ebody = get(base + "/energy", headers)
            if not _check_auth(ecode):
                energy = dig(ebody, "balance") if (200 <= ecode < 300) else None
        except Exception:
            pass

    # 连签天数：第 3 段已经取过 /streak，没补登过就直接复用，别在同一轮里打两次同一个接口。
    # 补登成功会改变天数（streak_stale），那时才有必要重新取。
    try:
        if streak_body is not None and not streak_stale:
            streak_obj = dig(streak_body, "streak") or {}
            streak_days = streak_obj.get("days") if isinstance(streak_obj, dict) else None
        elif _budget_left() > 0:
            scode2, sbody2 = get(base + "/streak", headers)
            if not _check_auth(scode2):
                streak_obj = dig(sbody2, "streak") or {}
                streak_days = streak_obj.get("days") if isinstance(streak_obj, dict) else None
    except Exception:
        pass

    tail = []
    if energy is not None:
        tail.append("能量 %s" % energy)
    if streak_days is not None:
        tail.append("连签 %s 天" % streak_days)
    if credits_gained:
        tail.append("本次 +共 %s 积分" % credits_gained)

    if parts:
        report = "；".join(parts)
    elif failures:
        report = "成长中心各步骤均失败"
    else:
        report = "成长中心无可领取项"
    if tail:
        report += "（%s）" % "，".join(tail)

    # 只有"确有需要关注的失败且一件都没成"才算整体失败。
    # 派 Buddy 已达每日上限这类 4xx 是每天的常态，不能让计划任务天天报红。
    result_code = 1 if (hard_failures and not successes) else 0
    # idle = 这一轮既没领到东西也没出错，纯空跑（Buddy 还在路上 / 今日名额已用完 /
    # 确实没有可领项）。轮询任务靠它决定要不要写日志。
    idle = (successes == 0 and failures == 0)
    return result_code, {"result": "GROWTH", "report": report, "credits_gained": credits_gained,
                         "energy": energy, "streak_days": streak_days, "idle": idle,
                         **({"failures": failures} if failures else {})}


def run_auto(headers, endpoint):
    """每日自动化主逻辑：查状态→未签才领→返回一行汇报。"""
    scode, sbody = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers, retry=True)

    if scode == CODE_BUDGET_OUT:
        return 1, {
            "result": "TIMEOUT",
            "report": "已达本次运行时间预算，签到跳过，下次自动重试",
        }
    if scode == CODE_NO_NETWORK:
        return 1, {
            "result": "NETWORK",
            "report": "网络不可达，签到跳过，下次自动重试（%s）" % (sbody.get("error") or ""),
            "error": sbody.get("error"),
        }
    if scode in (401, 403):
        return 1, {
            "result": "NO_SESSION",
            "report": "登录态已失效（HTTP %s），请重新登录 WorkBuddy 桌面端" % scode,
            "http": scode,
        }
    if not (200 <= scode < 300):
        return 1, {
            "result": "ERROR",
            "report": "签到接口返回异常（HTTP %s），请重新登录客户端或稍后重试" % scode,
            "http": scode,
            "status_body": sbody,
        }

    status = sbody if isinstance(sbody, dict) else {}
    active = dig(status, "active")
    activity_name = dig(status, "activity_name")

    if active is False:
        report = "签到活动未开启" + ("（%s）" % activity_name if activity_name else "")
        return 0, {"result": "INACTIVE", "report": report, "active": False}

    if dig(status, "today_checked_in") in (True, 1):
        return 0, _already_report(status)

    ccode, cbody = post(endpoint + "/v2/billing/meter/daily-checkin", headers, retry=True)

    if ccode in (CODE_NO_NETWORK, CODE_BUDGET_OUT):
        return 1, {
            "result": "NETWORK" if ccode == CODE_NO_NETWORK else "TIMEOUT",
            "report": "领取请求未能送达，下次自动重试（%s）" % (
                (cbody.get("error") or "") if isinstance(cbody, dict) else ""),
        }

    # 登录态判定要先于"已签"判定，避免失效时的报错体被误判为已领取
    if ccode in (401, 403):
        return 1, {
            "result": "NO_SESSION",
            "report": "登录态已失效（HTTP %s），请重新登录 WorkBuddy 桌面端" % ccode,
            "http": ccode,
        }

    if _is_already_checked_in(cbody):
        scode2, sbody2 = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers, retry=True)
        fresh = sbody2 if (200 <= scode2 < 300 and isinstance(sbody2, dict)) else status
        return 0, _already_report(fresh, via="今日已签过（服务端判定已领取）")

    credit = dig(cbody, "credit")
    if credit is not None:
        scode2, sbody2 = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers, retry=True)
        fresh = sbody2 if (200 <= scode2 < 300 and isinstance(sbody2, dict)) else status
        streak_days = dig(fresh, "streak_days") or dig(status, "streak_days")
        total_credits = dig(fresh, "total_credits")
        is_streak_day = dig(fresh, "is_streak_day")
        next_streak_day = dig(fresh, "next_streak_day")
        bonus = "，且为连签奖励日" if is_streak_day else ""
        cum = "，累计 %s 积分" % fmt_credit(total_credits) if total_credits is not None else ""
        # streak_days 缺失时不要把 None 打进文案
        streak = "（连续 %s 天%s）" % (streak_days, cum) if streak_days is not None else (
            "（%s）" % cum.lstrip("，") if cum else "")
        report = "成功领取 %s 积分%s%s" % (fmt_credit(credit), bonus, streak)
        return 0, {
            "result": "CLAIMED",
            "report": report,
            "credit": credit,
            "streak_days": streak_days,
            "total_credits": total_credits,
            "is_streak_day": is_streak_day,
            "next_streak_day": next_streak_day,
        }

    if isinstance(cbody, dict) and ("code" in cbody or "msg" in cbody):
        msg = cbody.get("msg") or ("code %s" % cbody.get("code"))
        return 1, {
            "result": "ERROR",
            "report": "领取失败：%s（HTTP %s）" % (msg, ccode),
            "http": ccode,
            "claim_body": cbody,
        }

    return 1, {
        "result": "UNKNOWN",
        "report": "未识别的领取返回，请检查接口：%s" % json.dumps(cbody, ensure_ascii=False)[:200],
        "http": ccode,
        "claim_body": cbody,
    }


def main():
    """薄壳：只负责取命令 + 兜住一切异常，保证 silent 模式下结果必定落盘。"""
    action = sys.argv[1] if len(sys.argv) > 1 else "auto"
    try:
        return _run(action)
    except Exception as e:
        # 无窗口运行下任何未捕获异常都会让当天的失败无痕消失，这里是最后一道防线
        emit({"result": "ERROR", "report": "脚本运行异常（%s: %s）" % (type(e).__name__, e)}, action)
        return 2


def _run(action):
    _start_budget(action)
    known = ("auto", "silent", "growth", "silent-growth", "status", "claim", "all")
    if action not in known:
        emit({"result": "ERROR",
              "report": "未知命令：%s（可用：%s）" % (action, " / ".join(known))},
             action)
        return 2

    auth_file, looked_in = find_auth_file()
    env_override = os.environ.get("WORKBUDDY_AUTH_FILE")
    if not auth_file or not os.path.exists(auth_file):
        if env_override:
            report = ("WORKBUDDY_AUTH_FILE 指向的文件不存在：%s" % env_override)
        else:
            report = ("未找到 WorkBuddy 登录凭据。请先在本机登录 WorkBuddy 桌面端；"
                      "或设置环境变量 WORKBUDDY_AUTH_FILE 指向 workbuddy-desktop.info。")
        emit({"result": "NO_AUTH", "report": report, "looked_in": looked_in}, action)
        return 2

    try:
        session = load_session_retry(auth_file)
    except json.JSONDecodeError as e:
        # JSONDecodeError 是 ValueError 的子类，必须先于下面的分支捕获，否则会被误归类
        emit({"result": "ERROR",
              "report": "登录凭据文件不是合法 JSON（%s），请重新登录 WorkBuddy 桌面端" % e},
             action)
        return 2
    except ValueError as e:
        # 文件编码损坏（UnicodeDecodeError 也是 ValueError 子类）等情形
        emit({"result": "ERROR",
              "report": "登录凭据文件内容损坏（%s: %s），请重新登录 WorkBuddy 桌面端" % (type(e).__name__, e)},
             action)
        return 2
    except Exception as e:
        # 无读取权限等其它 IO 问题
        emit({"result": "ERROR",
              "report": "读取登录凭据失败（%s: %s），请重新登录 WorkBuddy 桌面端" % (type(e).__name__, e)},
             action)
        return 2

    try:
        headers = build_headers(session)
    except ValueError as e:
        emit({"result": "NO_SESSION", "report": str(e)}, action)
        return 1

    endpoint = ((session.get("auth") or {}).get("endpoint") or DEFAULT_ENDPOINT).rstrip("/")

    if action in ("auto", "silent"):
        code, out = run_auto(headers, endpoint)
        # 网络本就不可达时不必再跑成长中心的一串请求（每个都要重试+等待），也免得
        # 汇报出"无可领取项"这种假的安心话
        if out.get("result") in ("NETWORK", "TIMEOUT"):
            out["growth"] = "网络不可达或时间预算耗尽，成长中心跳过"
            out["growth_result"] = out["result"]
            emit(out, action)
            return code
        # 签到后顺带跑成长中心；它出任何问题都不能吞掉签到已成功的事实
        try:
            gcode, gout = run_growth(headers, endpoint)
        except Exception as e:
            gcode, gout = 1, {"result": "ERROR",
                              "report": "成长中心异常（%s: %s）" % (type(e).__name__, e)}
        out["growth"] = gout.get("report")
        out["growth_result"] = gout.get("result")
        if gout.get("credits_gained"):
            out["report"] += "；" + gout["report"]
        # run_growth 只在"确有硬失败且一件都没成"时返回非 0（无可领取项、4xx 业务规则
        # 均返回 0），直接透传即可——之前按 result 枚举漏了 result=GROWTH 的整体失败
        if gcode != 0 and code == 0:
            code = gcode
        emit(out, action)
        return code

    if action == "growth":
        code, out = run_growth(headers, endpoint)
        emit(out, action)
        return code

    if action == "silent-growth":
        # 只跑成长中心，结果写日志文件（emit 按 silent 前缀判定落盘）。
        # 不碰签到接口：签到一天一次就够，轮询没必要反复打它。
        # 空跑默认不落盘——一天要跑好几轮，全写进去只会把真正有价值的记录
        # 淹没在一串"Buddy 旅行中"里。要看完整过程就设 WORKBUDDY_GROWTH_LOG_EMPTY=1。
        code, out = run_growth(headers, endpoint)
        if (not out.get("idle")) or _env_flag("WORKBUDDY_GROWTH_LOG_EMPTY"):
            emit(out, action)
        return code

    # 以下为交互式调试命令，输出原始返回。走 emit 而非 print，这样非法配置的
    # config_warning 同样能带出来（这些命令不会是 silent，仍然打到 stdout）
    if action in ("status", "all"):
        scode, sbody = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers, retry=True)
        emit({"step": "status", "http": scode, "body": sbody}, action)

    if action in ("claim", "all"):
        ccode, cbody = post(endpoint + "/v2/billing/meter/daily-checkin", headers, retry=True)
        emit({"step": "claim", "http": ccode, "body": cbody}, action)

    return 0

if __name__ == "__main__":
    sys.exit(main())

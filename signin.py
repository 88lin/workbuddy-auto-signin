"""WorkBuddy 每日签到自动领取脚本。

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
  python signin.py auto     # 每日自动化：签到 + 成长中心（领旅行礼物/派Buddy/开盲盒/领任务奖）
  python signin.py silent   # 同 auto，但结果写日志文件而非 stdout（配合 pythonw.exe 静默运行）
  python signin.py growth   # 仅成长中心（不签到）
  python signin.py status   # 仅查签到状态（调试）
  python signin.py claim    # 仅领取签到（调试，幂等）
  python signin.py all      # 查签到状态 + 领取（调试）
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

DEFAULT_ENDPOINT = "https://copilot.tencent.com"
AUTH_BASENAME = os.path.join("CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info")

# 伪 HTTP 码：区分"没拿到响应"的两种原因
CODE_NO_NETWORK = -1   # 连不上/超时
CODE_BUDGET_OUT = -2   # 本次运行的时间预算已耗尽，主动放弃后续请求

# 计划任务的 ExecutionTimeLimit 是 PT10M；跑满会被系统直接杀掉，届时 emit 还没执行，
# 当天日志整条丢失。这里自设更小的预算，确保总能走到写日志那一步。
# MAX_BUDGET_SECONDS 必须与 README 里那条 ExecutionTimeLimit 保持一致（PT10M = 600s，
# 留 60s 给解释器启动和收尾）；若你把定时任务的时限调大，这两处要一起改。
DEFAULT_BUDGET_SECONDS = 420.0
MAX_BUDGET_SECONDS = 540.0
REQUEST_TIMEOUT = 30
_started_at = None
_budget_seconds = DEFAULT_BUDGET_SECONDS
_config_warning = None   # 配置非法时的告警，由 emit 统一带进输出


def _parse_budget():
    """解析预算环境变量。非法值/越界值一律夹到安全区间——绝不能在这里抛异常。

    这段逻辑曾写在模块顶层，WORKBUDDY_BUDGET_SECONDS=abc 会让进程在 main() 的
    try/except 生效之前就崩掉，silent 模式下当天日志整条为空。
    """
    raw = os.environ.get("WORKBUDDY_BUDGET_SECONDS")
    if not raw:
        return DEFAULT_BUDGET_SECONDS, None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_SECONDS, "WORKBUDDY_BUDGET_SECONDS=%r 不是数字，已回落 %s 秒" % (
            raw, int(DEFAULT_BUDGET_SECONDS))
    if val <= 0:
        # "0" 是非空字符串，用 `or` 兜不住；且 0 会让每个请求都直接放弃，脚本永久失效
        return DEFAULT_BUDGET_SECONDS, "WORKBUDDY_BUDGET_SECONDS=%s 必须为正数，已回落 %s 秒" % (
            raw, int(DEFAULT_BUDGET_SECONDS))
    if val > MAX_BUDGET_SECONDS:
        # 上限同样是硬要求：预算大于任务时限就等于没有预算，黑洞式超时会把进程跑到被强杀
        return MAX_BUDGET_SECONDS, ("WORKBUDDY_BUDGET_SECONDS=%s 超过上限，已夹到 %s 秒"
                                    "（须小于定时任务的 ExecutionTimeLimit）") % (
            raw, int(MAX_BUDGET_SECONDS))
    return val, None


def _start_budget():
    """启动预算时钟；配置非法时记下告警，由 emit 带进输出而不是静默回落。"""
    global _started_at, _budget_seconds, _config_warning
    _budget_seconds, _config_warning = _parse_budget()
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


def _dumps(out):
    """序列化汇报内容；default=str 兜住意外混入的非 JSON 类型，绝不让唯一的输出通道崩掉。"""
    try:
        return json.dumps(out, ensure_ascii=False, default=str)
    except Exception:
        return repr(out)


def emit(out, action):
    """silent 模式写日志文件，其余模式打印到 stdout；本函数保证不抛异常。"""
    if _config_warning and isinstance(out, dict):
        out = dict(out, config_warning=_config_warning)
    payload = _dumps(out)
    if action != "silent":
        try:
            print(payload)
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
    """成长中心自动化：领旅行礼物→派 Buddy 出发→开盲盒→领任务奖→汇报。

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
        if travel == "idle":
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
            parts.append("Buddy 旅行中（%s）" % loc_name)
    except Exception as e:
        parts.append("旅行模块异常（%s: %s）" % (type(e).__name__, e))
        failures += 1
        hard_failures += 1

    # --- 2. 盲盒/抽奖 ---
    if _budget_left() <= 0:
        parts.append("时间预算耗尽，盲盒跳过")
    else:
        try:
            lcode, lbody = get(base + "/lottery/chances", headers)
            if _check_auth(lcode):
                return 1, {"result": "NO_SESSION",
                           "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
            chances = as_int(dig(lbody, "balance")) if (200 <= lcode < 300) else 0
            if chances > 0:
                dcode, dbody = post(base + "/lottery/draw", headers, {})
                if _check_auth(dcode):
                    return 1, {"result": "NO_SESSION",
                               "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
                if 200 <= dcode < 300:
                    prize = dig(dbody, "prize_name") or dig(dbody, "prize") or "未知"
                    parts.append("开盲盒获得：%s" % prize)
                    successes += 1
                else:
                    parts.append("开盲盒失败（HTTP %s）" % dcode)
                    failures += 1
                    hard_failures += _is_hard_failure(dcode)
        except Exception as e:
            parts.append("盲盒模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 3. 任务领奖 ---
    if _budget_left() <= 0:
        parts.append("时间预算耗尽，任务领奖跳过")
    else:
        try:
            tcode, tbody = get(base + "/tasks", headers)
            if _check_auth(tcode):
                return 1, {"result": "NO_SESSION",
                           "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
            if 200 <= tcode < 300:
                tasks = dig(tbody, "tasks") or []
                for t in tasks:
                    if _budget_left() <= 0:
                        parts.append("时间预算耗尽，剩余任务下次再领")
                        break
                    try:
                        prog = t.get("progress") or {}
                        target = as_int(prog.get("target"), 1) or 1  # target 为 0/缺失时按 1 处理，避免误判已完成
                        done = as_int(prog.get("current")) >= target
                        if done and t.get("accept_status") != "claimed" and t.get("has_reward"):
                            acode, abody = post(base + "/tasks/accept", headers,
                                                {"task_code": t.get("task_code")})
                            if _check_auth(acode):
                                return 1, {"result": "NO_SESSION",
                                           "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
                            if 200 <= acode < 300:
                                rc = as_int(t.get("reward_credit"))
                                re_ = as_int(t.get("reward_energy"))
                                credits_gained += rc
                                parts.append("领任务奖「%s」+credit%s+energy%s" % (
                                    t.get("title", t.get("task_code")), rc, re_))
                                successes += 1
                    except Exception as e:
                        parts.append("任务「%s」异常（%s: %s）" % (
                            t.get("task_code", "?"), type(e).__name__, e))
                        failures += 1
                        hard_failures += 1
        except Exception as e:
            parts.append("任务模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 4. 能量 & 连签状态（纯展示值，预算不够就直接不取，不计失败）---
    energy = None
    streak_days = None
    if _budget_left() > 0:
        try:
            ecode, ebody = get(base + "/energy", headers)
            if not _check_auth(ecode):
                energy = dig(ebody, "balance") if (200 <= ecode < 300) else None
        except Exception:
            pass

    if _budget_left() > 0:
        try:
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
    return result_code, {"result": "GROWTH", "report": report, "credits_gained": credits_gained,
                         "energy": energy, "streak_days": streak_days,
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
    _start_budget()
    known = ("auto", "silent", "growth", "status", "claim", "all")
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
        session = load_session(auth_file)
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

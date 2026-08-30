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
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "https://copilot.tencent.com"
AUTH_BASENAME = os.path.join("CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info")


def find_auth_file():
    """按平台探测 WorkBuddy 桌面端写出的登录凭据文件，支持环境变量覆盖。"""
    override = os.environ.get("WORKBUDDY_AUTH_FILE")
    if override:
        return override
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
            return c
    return None


def load_session(auth_file):
    with open(auth_file, "r", encoding="utf-8") as f:
        return json.load(f)


def build_headers(session):
    auth = session.get("auth") or {}
    account = session.get("account") or {}
    token = auth.get("accessToken")
    uid = account.get("uid")
    if not token or not uid:
        raise SystemExit("NO_SESSION: 本地未找到有效登录会话")
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


def _request(url, headers, method="GET", payload=None):
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
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


def post(url, headers, payload=None):
    return _request(url, headers, method="POST", payload=payload)


def get(url, headers):
    return _request(url, headers, method="GET")


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
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


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
    """成长中心自动化：领旅行礼物→派 Buddy 出发→开盲盒→领任务奖→汇报。"""
    base = endpoint + "/v2/activity/growth"
    parts = []
    credits_gained = 0

    # --- 1. Buddy 旅行：领礼物 + 派出发 ---
    scode, sbody = get(base + "/buddy/travel/status", headers)
    travel = dig(sbody, "state") if (200 <= scode < 300) else None

    if scode in (401, 403):
        return 1, {"result": "NO_SESSION",
                   "report": "登录态已失效，请重新登录 WorkBuddy 桌面端"}
    if travel == "arrived":
        record_id = dig(sbody, "record_id")
        reward = dig(sbody, "reward_credit") or 0
        ccode, cbody = post(base + "/buddy/travel/claim", headers, {"record_id": record_id})
        if 200 <= ccode < 300 and dig(cbody, "reward_credit") is not None:
            got = dig(cbody, "reward_credit")
            credits_gained += got
            parts.append("领旅行礼物 +%s 积分" % fmt_credit(got))
        else:
            parts.append("领旅行礼物失败（HTTP %s）" % ccode)
        travel = "idle"  # 领完后变 idle
    if travel == "idle":
        ccode, cbody = get(base + "/buddy/travel/config", headers)
        locs = dig(cbody, "locations") if (200 <= ccode < 300) else None
        if locs:
            loc = locs[0]
            dcode, dbody = post(base + "/buddy/travel/depart", headers, {"location_id": loc.get("id")})
            if 200 <= dcode < 300:
                loc_name = (dig(dbody, "location") or {}).get("name", "?")
                dur = dig(dbody, "duration_hours") or (dig(dbody, "location") or {}).get("duration_hours", "?")
                parts.append("派 Buddy 去%s（%s 小时后回）" % (loc_name, dur))
            else:
                msg = dig(dbody, "msg") or ""
                parts.append("派 Buddy 失败：%s" % (msg or ("HTTP %s" % dcode)))
    elif travel == "traveling":
        loc_name = (dig(sbody, "location") or {}).get("name", "?")
        parts.append("Buddy 旅行中（%s）" % loc_name)

    # --- 2. 盲盒/抽奖 ---
    lcode, lbody = get(base + "/lottery/chances", headers)
    chances = dig(lbody, "balance") if (200 <= lcode < 300) else 0
    if chances and chances > 0:
        dcode, dbody = post(base + "/lottery/draw", headers, {})
        if 200 <= dcode < 300:
            prize = dig(dbody, "prize_name") or dig(dbody, "prize") or "未知"
            parts.append("开盲盒获得：%s" % prize)
        else:
            parts.append("开盲盒失败（HTTP %s）" % dcode)

    # --- 3. 任务领奖 ---
    tcode, tbody = get(base + "/tasks", headers)
    if 200 <= tcode < 300:
        tasks = dig(tbody, "tasks") or []
        for t in tasks:
            done = (t.get("progress") or {}).get("current", 0) >= (t.get("progress") or {}).get("target", 1)
            if done and t.get("accept_status") != "claimed" and t.get("has_reward"):
                acode, abody = post(base + "/tasks/accept", headers, {"task_code": t.get("task_code")})
                if 200 <= acode < 300:
                    rc = t.get("reward_credit", 0)
                    re_ = t.get("reward_energy", 0)
                    credits_gained += rc
                    parts.append("领任务奖「%s」+credit%s+energy%s" % (t.get("title", t.get("task_code")), rc, re_))

    # --- 4. 能量 & 连签状态 ---
    ecode, ebody = get(base + "/energy", headers)
    energy = dig(ebody, "balance") if (200 <= ecode < 300) else None

    scode2, sbody2 = get(base + "/streak", headers)
    streak_obj = dig(sbody2, "streak") or {}
    streak_days = streak_obj.get("days") if isinstance(streak_obj, dict) else None

    tail = []
    if energy is not None:
        tail.append("能量 %s" % energy)
    if streak_days is not None:
        tail.append("连签 %s 天" % streak_days)
    if credits_gained:
        tail.append("本次 +共 %s 积分" % credits_gained)

    report = "；".join(parts) if parts else "成长中心无可领取项"
    if tail:
        report += "（%s）" % "，".join(tail)
    return 0, {"result": "GROWTH", "report": report, "credits_gained": credits_gained,
               "energy": energy, "streak_days": streak_days}


def run_auto(headers, endpoint):
    """每日自动化主逻辑：查状态→未签才领→返回一行汇报。"""
    scode, sbody = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers)

    if scode in (401, 403):
        return 1, {
            "result": "NO_SESSION",
            "report": "登录态已失效（HTTP %s），请重新登录 WorkBuddy 桌面端" % scode,
            "http": scode,
        }
    if not (200 <= scode < 300):
        return 1, {
            "result": "ERROR",
            "report": "签到接口返回异常（HTTP %s），可能登录态失效，请重新登录客户端" % scode,
            "http": scode,
            "status_body": sbody,
        }

    status = sbody if isinstance(sbody, dict) else {}
    active = dig(status, "active")
    activity_name = dig(status, "activity_name")

    if active is False:
        report = "签到活动未开启" + ("（%s）" % activity_name if activity_name else "")
        return 0, {"result": "INACTIVE", "report": report, "active": False}

    if dig(status, "today_checked_in") is True:
        return 0, _already_report(status)

    ccode, cbody = post(endpoint + "/v2/billing/meter/daily-checkin", headers)

    if _is_already_checked_in(cbody):
        scode2, sbody2 = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers)
        fresh = sbody2 if (200 <= scode2 < 300 and isinstance(sbody2, dict)) else status
        return 0, _already_report(fresh, via="今日已签过（服务端判定已领取）")

    if ccode in (401, 403):
        return 1, {
            "result": "NO_SESSION",
            "report": "登录态已失效（HTTP %s），请重新登录 WorkBuddy 桌面端" % ccode,
            "http": ccode,
        }

    credit = dig(cbody, "credit")
    if credit is not None:
        scode2, sbody2 = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers)
        fresh = sbody2 if (200 <= scode2 < 300 and isinstance(sbody2, dict)) else status
        streak_days = dig(fresh, "streak_days") or dig(status, "streak_days")
        total_credits = dig(fresh, "total_credits")
        is_streak_day = dig(fresh, "is_streak_day")
        next_streak_day = dig(fresh, "next_streak_day")
        bonus = "，且为连签奖励日" if is_streak_day else ""
        cum = "，累计 %s 积分" % fmt_credit(total_credits) if total_credits is not None else ""
        report = "成功领取 %s 积分%s（连续 %s 天%s）" % (fmt_credit(credit), bonus, streak_days, cum)
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
    action = sys.argv[1] if len(sys.argv) > 1 else "auto"

    auth_file = find_auth_file()
    if not auth_file or not os.path.exists(auth_file):
        home = os.path.expanduser("~")
        guesses = [
            os.path.join(os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local")), AUTH_BASENAME),
            os.path.join(home, "Library", "Application Support", AUTH_BASENAME),
        ]
        out = {
            "result": "NO_AUTH",
            "report": "未找到 WorkBuddy 登录凭据。请先在本机登录 WorkBuddy 桌面端；"
                      "或设置环境变量 WORKBUDDY_AUTH_FILE 指向 workbuddy-desktop.info。",
            "looked_in": guesses,
        }
        print(json.dumps(out, ensure_ascii=False))
        return 2

    session = load_session(auth_file)
    headers = build_headers(session)
    endpoint = ((session.get("auth") or {}).get("endpoint") or DEFAULT_ENDPOINT).rstrip("/")

    if action in ("auto", "silent"):
        code, out = run_auto(headers, endpoint)
        # 签到后顺带跑成长中心
        gcode, gout = run_growth(headers, endpoint)
        out["growth"] = gout.get("report")
        if gout.get("credits_gained"):
            out["report"] += "；" + gout["report"]

        if action == "silent":
            # 写日志文件（配合 pythonw.exe 无窗口运行，不输出 stdout）
            log_path = os.environ.get("WORKBUDDY_SIGNIN_LOG")
            if not log_path:
                log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signin.log")
            from datetime import datetime
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write("[%s] %s\n" % (ts, json.dumps(out, ensure_ascii=False)))
        else:
            print(json.dumps(out, ensure_ascii=False))
        return code

    if action == "growth":
        code, out = run_growth(headers, endpoint)
        print(json.dumps(out, ensure_ascii=False))
        return code

    if action in ("status", "all"):
        scode, sbody = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers)
        print(json.dumps({"step": "status", "http": scode, "body": sbody}, ensure_ascii=False))

    if action in ("claim", "all"):
        ccode, cbody = post(endpoint + "/v2/billing/meter/daily-checkin", headers)
        print(json.dumps({"step": "claim", "http": ccode, "body": cbody}, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())

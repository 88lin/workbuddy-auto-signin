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
  python signin.py auto     # 每日自动化：查状态→未签才领→输出一行汇报
  python signin.py status   # 仅查状态（调试）
  python signin.py claim    # 仅领取（调试，幂等）
  python signin.py all      # 查状态 + 领取（调试）
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


def post(url, headers, payload=None):
    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:500]}


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

    if action == "auto":
        code, out = run_auto(headers, endpoint)
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

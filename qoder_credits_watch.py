#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qoder CN 每日 100 Credits 的看门狗（Windows 计划任务入口）。

上游 qoder_signin.py 的 silent 模式只把失败写进 qoder_signin.log，
没人看就等于静默失效。本文件在它外面包一层：

 1. 领取失败（登录态过期 / 凭据读不到 / 接口异常）→ 手机推送，不再只写日志
 2. 登录态临近过期（默认 7 天内）→ 提前提醒一次/天
 3. 恢复正常时补一条「已恢复」
 4. 每次运行都写一行审计日志 + 心跳文件，便于反查它到底跑没跑

由 pythonw.exe 拉起，无窗口。计划任务动作为：
    pythonw.exe "D:\\Sandbox\\workbuddy-auto-signin\\qoder_credits_watch.py"

手动测试推送：python qoder_credits_watch.py --test
"""

import io
import json
import os
import sys
import traceback
from contextlib import redirect_stdout
from datetime import datetime, timezone

import bark_notify

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "qoder-watch-config.json")
STATE_PATH = os.path.join(HERE, "qoder-watch-state.json")
LOG_PATH = os.path.join(HERE, "qoder-watch.log")
HEARTBEAT_PATH = os.path.join(HERE, "qoder-watch-heartbeat.json")

DEFAULT_CONFIG = {
    # 首选 Bark：走 APNs，iPhone 上不依赖后台常驻。
    # 告警统一使用 timeSensitive，不使用 critical。
    # 单台设备填 bark_key；多台设备填 bark_keys（数组），会全部推送。
    "bark_server": "https://api.day.app",
    "bark_key": "",
    "bark_keys": [],
    # 想给某台设备单独设分组时用：
    # "bark_extra": [{"key": "另一台的key", "group": "Qoder 提醒（iPad）"}],
    "bark_extra": [],
    "bark_group": "签到与额度",
    "expiry_warn_days": 7,
    "notify_on_claim": False,
}

URGENT_KINDS = ("AUTH_REJECTED", "NO_AUTH", "AUTH_ERROR", "ERROR")
OK_RESULTS = ("CLAIMED", "ALREADY", "INACTIVE", "DRY_RUN")


def now():
    return datetime.now()


def stamp():
    return now().strftime("%Y-%m-%d %H:%M:%S")


def log(line):
    """审计日志：每次运行一行，谁也别想悄悄失败。"""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write("[%s] %s\n" % (stamp(), line))
    except Exception:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else dict(default)
    except Exception:
        return dict(default)


def save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        log("写 %s 失败：%s" % (os.path.basename(path), exc))


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_PATH, {}))
    if not os.path.exists(CONFIG_PATH):
        save_json(CONFIG_PATH, cfg)
    return cfg


def push(cfg, title, body, priority=4, subtitle=None):
    """发到所有 Bark 设备；失败设备会被记录，其余设备仍继续尝试。"""
    def record(key, ok, sent_title):
        log("Bark 推送%s：%s" % ("成功" if ok else "失败", sent_title))

    ok = bark_notify.push(cfg, title, body, level="active" if priority <= 3 else "timeSensitive",
                          subtitle=subtitle, on_result=record)
    if not bark_notify.targets(cfg):
        log("没有可用的 Bark 设备，消息只落日志：" + title)
    return ok



def run_signin():
    """跑一次 auto，把它 print 出来的那行 JSON 抓回来。"""
    sys.path.insert(0, HERE)
    import qoder_signin  # 上游脚本，同目录

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = qoder_signin.run("auto")
    raw = buf.getvalue().strip()
    if not raw:
        return code, {"result": "ERROR", "report": "上游脚本没有输出 JSON"}
    try:
        return code, json.loads(raw.splitlines()[-1])
    except ValueError:
        return code, {"result": "ERROR", "report": "输出无法解析：%s" % raw[:200]}


def days_left(out):
    value = out.get("token_expires_at")
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        expires = datetime.fromisoformat(text)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return (expires - datetime.now(timezone.utc)).days
    except Exception:
        return None


def fmt_expiry(out):
    value = out.get("token_expires_at")
    if not value:
        return "未知"
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def notify_once(cfg, state, kind, title, body, priority=4, subtitle=None):
    """同一种提醒一天最多推一次，避免 5 次触发把手机刷屏。"""
    today = now().strftime("%Y-%m-%d")
    last = state.get("last_alert") or {}
    if last.get("kind") == kind and last.get("date") == today:
        return False
    if push(cfg, title, body, priority=priority, subtitle=subtitle):
        state["last_alert"] = {"kind": kind, "date": today}
        return True
    return False


def main(argv):
    argv = list(argv or [])
    cfg = load_config()
    state = load_json(STATE_PATH, {})

    if "--test" in argv:
        ok = push(
            cfg,
            "Qoder · 推送测试",
            "测试 Bark 通知。登录异常会以时效性提醒显示。",
            priority=5,
            subtitle="Qoder CN · Windows",
        )
        print("test push (Bark):", "ok" if ok else "failed")
        return 0 if ok else 1

    try:
        code, out = run_signin()
    except Exception:
        detail = traceback.format_exc(limit=3).strip().splitlines()[-1]
        out = {"result": "ERROR", "report": "看门狗拉起上游脚本失败：%s" % detail}
        code = 2

    result = str(out.get("result") or "UNKNOWN")
    report = str(out.get("report") or "").strip()
    left = days_left(out)
    warned = None

    if result in URGENT_KINDS:
        if result == "AUTH_REJECTED":
            title = "Qoder · 需要重新登录"
            body = (
                "今日 Credits 签到因登录状态失效而未完成。请打开 Qoder CN 检查登录状态并重新登录；"
                "看门狗会在下次计划运行时重试。登录状态到期时间：%s。" % fmt_expiry(out)
            )
        elif result == "NO_AUTH":
            title = "Qoder · 未找到登录状态"
            body = (
                "今日 Credits 签到未执行。请确认已安装 Qoder CN，并在桌面端登录；"
                "看门狗会在下次计划运行时重试。"
            )
        elif result == "AUTH_ERROR":
            title = "Qoder · 登录状态读取失败"
            body = "今日 Credits 签到未完成。请打开 Qoder CN 检查登录状态；若问题持续，请重新登录后再观察下次运行。"
        else:
            title = "Qoder · 签到运行异常"
            body = "今日 Credits 签到可能未完成。请检查 Qoder CN 和看门狗日志，再确认下次计划运行是否恢复。"
        if notify_once(cfg, state, result, title, body, priority=5, subtitle="Qoder CN · Windows"):
            warned = result

    elif left is not None and left <= int(cfg.get("expiry_warn_days") or 7) and left >= 0:
        title = "Qoder · 登录状态即将过期"
        body = (
            "登录状态约 %d 天后到期（%s）。请打开 Qoder CN 检查登录状态，必要时重新登录；"
            "过期后每日 Credits 签到可能停止。" % (left, fmt_expiry(out))
        )
        if notify_once(cfg, state, "EXPIRING", title, body, priority=5, subtitle="Qoder CN · Windows"):
            warned = "EXPIRING"

    last = state.get("last_alert") or {}
    if result in OK_RESULTS and last.get("kind") in URGENT_KINDS:
        # 上一次是坏的、这次好了：补一条恢复通知，免得你以为还坏着。
        # 用 notify_once 去重，同一天内失败/恢复反复横跳最多各推一次。
        if notify_once(
            cfg,
            state,
            "RECOVERED",
            "Qoder · 签到已恢复",
            "看门狗已重新完成一次正常运行（%s）。" % result,
            priority=3,
            subtitle="Qoder CN · Windows",
        ):
            warned = "RECOVERED"

    if result in OK_RESULTS:
        state["last_success"] = stamp()
    state["last_result"] = result
    state["last_report"] = report[:300]
    save_json(STATE_PATH, state)
    save_json(HEARTBEAT_PATH, {"last_run": stamp(), "result": result, "exit": code})

    log("result=%s exit=%s days_left=%s alert=%s report=%s"
        % (result, code, left, warned or "none", report[:200]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        log("看门狗自身崩溃：" + traceback.format_exc(limit=3).replace("\n", " | "))
        sys.exit(2)

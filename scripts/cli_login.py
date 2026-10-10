#!/usr/bin/env python3
"""在伪终端里驱动 codebuddy 的 /login，拿到登录 URL 并等待浏览器授权回调。

为什么需要这个脚本
------------------
云端沙箱的登录态是平台用 ACC_PRODUCT_CONFIG_V2/V3（type=custom-token）注入的，
CLI 走注入模式，不会把凭据写进文件，签到脚本因此报 NO_AUTH。
把这两个变量摘掉后，CLI 会退回 product.json 里的 cli-external-link 流程：
生成登录 URL -> 用户在浏览器授权 -> 凭据落到
~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info

为什么必须用伪终端
------------------
`codebuddy -p "/login"` 只会回一句 "Authentication required. Please use /login"，
登录命令在非交互模式下不生效；必须进 TUI 敲斜杠命令。

用法
----
    python3 cli_login.py [--wait 1500] [--cwd /workspace/workbuddy-auto-signin]

输出（stdout，逐行）：
    LOGIN_URL=<完整登录链接>      把这一行发给用户，让他在浏览器里打开登录
    CREDENTIAL_READY=<路径>       授权成功、凭据已落地
    EXIT_PID=... url=... cred=... 进程结束时的状态
"""

import argparse
import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time

DEFAULT_CRED = os.path.expanduser(
    "~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info")
DEFAULT_CWD = "/workspace/workbuddy-auto-signin"
# 平台注入的登录态变量，摘掉才会走正常的浏览器授权流程
DROP_ENV = ("ACC_PRODUCT_CONFIG_V2", "ACC_PRODUCT_CONFIG_V3", "X_IDE_AUTH_PROXY",
            "IDE_EDITOR_SERVER_AUTH_TYPE")
# TUI 时间线（秒）：先过「是否信任此目录」，再敲 /login，最后回车选「中国站」。
# 必须用 \r —— 这是 TUI 认的回车键，\n 无效。
KEYS = ((8.0, b"\r"), (14.0, b"/login\r"), (22.0, b"\r"))
COLS = 250  # 窗口太窄会把登录 URL 折行截断
ROWS = 60


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=float, default=1500.0, help="等待用户授权的秒数")
    ap.add_argument("--cwd", default=DEFAULT_CWD)
    ap.add_argument("--cred", default=DEFAULT_CRED)
    ap.add_argument("--log", default="/tmp/cli_login_out.txt")
    args = ap.parse_args()

    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

    proc = subprocess.Popen(["codebuddy"], stdin=slave, stdout=slave, stderr=slave,
                            env=env, cwd=args.cwd, preexec_fn=os.setsid)
    os.close(slave)

    started = time.time()
    sent = set()
    buf = b""
    url = None
    log = open(args.log, "wb", buffering=0)
    print("PID=%s waiting up to %.0fs for authorization" % (proc.pid, args.wait), flush=True)

    try:
        while time.time() - started < args.wait:
            elapsed = time.time() - started
            for at, key in KEYS:
                if elapsed >= at and at not in sent:
                    os.write(master, key)
                    sent.add(at)
            ready, _, _ = select.select([master], [], [], 0.5)
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                buf += data
                log.write(data)
            if url is None:
                text = re.sub(rb"\x1b\[[0-9;?]*[a-zA-Z]", b"", buf)
                found = re.findall(rb"https://\S*copilot\.tencent\.com/login\?[^\s]*", text)
                if found:
                    url = max(found, key=len).decode("utf-8", "replace")
                    print("LOGIN_URL=%s" % url, flush=True)
            if os.path.exists(args.cred):
                print("CREDENTIAL_READY=%s" % args.cred, flush=True)
                return 0
            if proc.poll() is not None and not ready:
                break
        print("TIMEOUT url=%s cred=%s" % (bool(url), os.path.exists(args.cred)), flush=True)
        return 1 if not os.path.exists(args.cred) else 0
    finally:
        print("EXIT_PID=%s url=%s cred=%s" % (proc.pid, bool(url), os.path.exists(args.cred)),
              flush=True)
        try:
            proc.terminate()
        except OSError:
            pass
        log.close()


if __name__ == "__main__":
    sys.exit(main())

"""看守循环：消费命令队列、定时刷新余额、自动跟随登录变化。"""
import os
import threading
import time
import traceback

from . import config, service
from .state import SingleInstance, commands, log, now, read_state, write_state
from .web import create_server


def startup_recovery(current):
    """处理上次异常退出遗留状态。

    只做告警，不自动重放换号；换号是不可逆的外部操作，必须由人确认。
    暂停状态下跳过启动补跑——这也是安全重启流程的基础。
    """
    state = read_state()
    if state.get("status") == config.SWITCHING_STATUS:
        log("WARNING: previous session ended while switching; check last_backup before retrying")
        write_state(status="error", last_error="上次换号未正常结束，请检查备份与登录状态")
    if state.get("paused"):
        log("monitoring paused on startup; skipping session catch-up")
        return
    counts = service.session_counts()
    if any(uid != current and number > 0 for uid, number in counts.items()):
        log("startup catch-up: sessions under another account")
        ok, detail = service.switch_with_clients_closed(current)
        write_state(status="watching" if ok else "error",
                    last_error="" if ok else detail,
                    last_action="startup session catch-up")


def daemon():
    """常驻主循环。单实例锁保证重复启动直接退出。"""
    with SingleInstance(config.LOCK):
        server = None
        try:
            log("=== hub started (pid %d) ===" % os.getpid())
            # 刻意不重置 paused：运维暂停状态必须跨重启保留。
            write_state(status="starting", pid=os.getpid(),
                        last_action="hub started", last_error="")
            try:
                server = create_server()
                threading.Thread(target=server.serve_forever, daemon=True).start()
                log("dashboard: http://127.0.0.1:%d/" % config.PORT)
            except Exception as exc:
                log("dashboard HTTP server failed: %s" % exc)

            service.refresh_balances()
            current = service.current_uid()
            while current is None:
                time.sleep(config.POLL_SECONDS)
                current = service.current_uid()

            startup_recovery(current)
            paused = bool(read_state().get("paused"))
            write_state(status="paused" if paused else "watching", current_uid=current)
            log("baseline uid: %s" % current)

            next_balance = time.time() + config.BALANCE_TTL
            while True:
                try:
                    time.sleep(config.POLL_SECONDS)

                    for action, payload in commands.drain():
                        if action == "balance":
                            service.refresh_balances(force=True)
                        elif action == "switch":
                            log("queued switch requested: %s" % payload)
                            ok, _ = service.do_switch(payload)
                            if ok:
                                current = service.current_uid() or current
                                next_balance = time.time() + config.BALANCE_TTL
                                service.refresh_balances(force=True)
                        elif action == "sync":
                            log("queued session sync requested")
                            service.do_sync()

                    if time.time() >= next_balance:
                        service.refresh_balances()
                        next_balance = time.time() + config.BALANCE_TTL

                    if read_state().get("paused"):
                        continue

                    uid = service.current_uid()
                    if not uid or uid == current:
                        continue
                    log("login changed: %s -> %s" % (current, uid))
                    time.sleep(config.SETTLE_SECONDS)
                    settled = service.current_uid()
                    if not settled or settled == current:
                        log("login state unsettled, skip")
                        continue
                    ok, detail = service.switch_with_clients_closed(settled)
                    write_state(status="watching" if ok else "error",
                                current_uid=settled, last_error="" if ok else detail,
                                last_action="auto-follow after account switch",
                                last_sync=now() if ok else None)
                    current = settled
                    log("cooldown %ds; watching uid %s" % (config.COOLDOWN_SECONDS, current))
                    time.sleep(config.COOLDOWN_SECONDS)
                except Exception:
                    log("loop error:\n" + traceback.format_exc())
                    write_state(status="error", last_error=traceback.format_exc()[-500:])
                    time.sleep(config.POLL_SECONDS)
        finally:
            if server is not None:
                server.shutdown()

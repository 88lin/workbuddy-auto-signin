"""业务编排：账号切换、会话跟随、状态聚合、余额缓存。

本模块不接触 HTTP。所有外部 Effect 通过 clients / signin / 文件系统完成，
测试时替换对应模块或函数即可，无需真实客户端与网络。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import signin

from . import billing, clients, config
from .state import SnapshotCache, log, now, read_state, write_state

_state_cache = SnapshotCache()


def invalidate_state_cache():
    _state_cache.invalidate()


def account_entries():
    """返回 (账号列表, active 名)；注册表只读一次，避免重复磁盘 IO 与快照漂移。"""
    try:
        registry = signin.load_registry()
        return registry.get("accounts") or [], registry.get("active")
    except Exception as exc:
        log("load_registry failed: %s" % exc)
        return [], None


def account_name_for(uid, entries=None):
    if not uid:
        return ""
    entries = entries if entries is not None else account_entries()[0]
    for entry in entries:
        if entry.get("uid") == uid:
            return entry["name"]
    return ""


def current_uid():
    """读客户端快照 uid；短暂重试覆盖原子替换窗口。"""
    for _ in range(5):
        try:
            with open(config.SNAPSHOT, encoding="utf-8") as handle:
                uid = (json.load(handle).get("primary") or {}).get("uid")
            if uid:
                return str(uid)
        except (OSError, ValueError, AttributeError):
            pass
        time.sleep(0.5)
    return None


def session_counts():
    try:
        uri = "file:" + str(config.DB_PATH).replace("\\", "/") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            return {row[0]: row[1] for row in conn.execute(
                "SELECT user_id, COUNT(*) FROM sessions GROUP BY user_id")}
        finally:
            conn.close()
    except Exception as exc:
        log("session_counts failed: %s" % exc)
        return {}


def run_migration(src, dst):
    cmd = [sys.executable, str(config.MIGRATE), "--source", src, "--target", dst, "--yes"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=900,
                                creationflags=config.NO_WINDOW)
    except Exception as exc:
        log("migrate.py failed to launch: %s" % exc)
        return False, str(exc)
    for line in [item.strip() for item in (result.stdout or "").splitlines() if item.strip()][-12:]:
        log("    " + line)
    if result.returncode not in (0, 3):
        detail = (result.stderr or "").strip()[:500] or "exit=%s" % result.returncode
        log("migrate.py FAILED: %s" % detail)
        return False, detail
    return True, "exit=%s" % result.returncode


def migrate_sessions_closed(target_uid):
    """调用前客户端必须已关闭。把所有其他账号的会话归到 target_uid。"""
    counts = session_counts()
    sources = [uid for uid, number in sorted(counts.items(), key=lambda kv: -kv[1])
               if uid != target_uid and number > 0]
    if not sources:
        log("no sessions to consolidate into %s" % target_uid)
        return True, "no sessions to move"
    ok_all, detail = True, ""
    for src in sources:
        log("migrating sessions %s -> %s" % (src, target_uid))
        ok, detail = run_migration(src, target_uid)
        ok_all = ok_all and ok
    return ok_all, detail


def refresh_balances(force=False):
    """余额查询串行 + 5 分钟缓存，既避免风控又避免面板轮询放大请求。"""
    state = read_state()
    stamp = state.get("balances_at", "")
    if not force and stamp:
        try:
            age = (datetime.now() - datetime.strptime(stamp, config.STATE_TIME_FORMAT)).total_seconds()
            if 0 <= age < config.BALANCE_TTL and state.get("balances"):
                return state["balances"]
        except ValueError:
            pass
    entries, _ = account_entries()
    log("refreshing balances for %d account(s)" % len(entries))
    balances = billing.fetch_balances(entries)
    write_state(balances=balances, balances_at=now())
    invalidate_state_cache()
    return balances


def switch_allowed():
    """风控保护：两次换号之间至少隔 MIN_SWITCH_INTERVAL 秒。"""
    last = read_state().get("last_switch_at")
    if not last:
        return True, ""
    try:
        age = (datetime.now() - datetime.strptime(last, config.STATE_TIME_FORMAT)).total_seconds()
    except ValueError:
        return True, ""
    if 0 <= age < config.MIN_SWITCH_INTERVAL:
        return False, "距上次换号仅 %d 秒，需间隔 %d 秒（防频繁换号风控）" % (
            int(age), config.MIN_SWITCH_INTERVAL)
    return True, ""


def _fail(message, action=None):
    log(message)
    write_state(status="error", last_error=message, last_action=action or "switch failed")
    return False, message


def do_switch(name):
    """冷切换客户端账号 + 会话跟随。返回 (ok, message)。

    失败路径统一写 error 状态并保留备份路径，便于人工回滚。
    """
    allowed, reason = switch_allowed()
    if not allowed:
        log("switch rejected: %s" % reason)
        write_state(status="error", last_error=reason, last_action="switch rejected")
        return False, reason

    entries, _ = account_entries()
    entry = next((item for item in entries if item["name"] == name), None)
    if not entry:
        return False, "未绑定账号「%s」" % name

    credential_path = signin.credential_path(entry)
    if not os.path.exists(credential_path):
        return False, "账号「%s」的凭据快照不存在" % name
    try:
        with open(credential_path, encoding="utf-8") as handle:
            target_uid = str((json.load(handle).get("account") or {}).get("uid") or "")
    except Exception as exc:
        return False, "读取凭据失败：%s" % exc
    if not target_uid:
        return False, "凭据里没有 uid"
    if target_uid != entry.get("uid"):
        return False, "凭据 uid 与清单记录不一致，拒绝切换"

    old_uid = current_uid()
    if old_uid == target_uid:
        write_state(status=config.SWITCHING_STATUS,
                    last_action="switch to %s (already logged in)" % name)
        ok, detail = switch_with_clients_closed(target_uid)
        write_state(status="watching" if ok else "error",
                    last_error="" if ok else detail,
                    last_action="sync on already-current account")
        invalidate_state_cache()
        return ok, detail or "already logged in"

    write_state(status=config.SWITCHING_STATUS,
                last_action="switching to %s" % name, last_error="")
    log("switch: %s -> %s (%s)" % (old_uid, target_uid, name))
    ok, instances = clients.close_clients()
    if not ok:
        return _fail("无法关闭 WorkBuddy 客户端，已取消切换")

    auth_file, _ = signin.find_auth_file()
    if not auth_file:
        return _fail("找不到客户端凭据文件 workbuddy-desktop.info")
    config.BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = config.BACKUPS / ("workbuddy-desktop.info.before-%s-%s" % (name, stamp))
    try:
        shutil.copy2(auth_file, backup)
        marker = Path(str(auth_file) + ".logged-out")
        if marker.exists():
            marker.unlink()
    except Exception as exc:
        return _fail("备份/清理登出标记失败，已取消切换：%s" % exc)

    try:
        shutil.copy2(credential_path, auth_file)
        with open(auth_file, encoding="utf-8") as handle:
            written = str((json.load(handle).get("account") or {}).get("uid") or "")
        if written != target_uid:
            raise RuntimeError("写回后 uid 校验失败：%s != %s" % (written, target_uid))
    except Exception as exc:
        return _fail("写回凭据失败：%s；备份在 %s" % (exc, backup))

    log("auth backed up -> %s" % backup.name)
    ok, detail = migrate_sessions_closed(target_uid)
    clients.reopen_clients(instances)
    time.sleep(10)
    verified = current_uid()
    if verified != target_uid:
        return _fail("客户端启动后登录 uid 校验失败（%s != %s），请用备份 %s 回滚" % (
            verified, target_uid, backup.name), action="switch to %s" % name)

    try:
        signin.use_account(name)
    except Exception as exc:
        log("use_account sync warning: %s" % exc)

    previous_switch = read_state().get("last_switch_at")
    write_state(status="watching" if ok else "error",
                current_uid=target_uid, counts=session_counts(),
                clients=clients.running_clients(),
                last_error="" if ok else detail,
                last_action="switched to %s" % name,
                last_sync=now() if ok else None,
                last_backup=str(backup),
                last_switch_at=now() if ok else previous_switch)
    invalidate_state_cache()
    log("switch done: %s, sessions_ok=%s" % (name, ok))
    return ok, detail or "switched to %s" % name


def switch_with_clients_closed(target_uid):
    """关客户端 -> 搬会话 -> 重开。供手动 sync / 自动跟随复用。"""
    ok, instances = clients.close_clients()
    if not ok:
        return False, "无法关闭 WorkBuddy 客户端"
    result, detail = migrate_sessions_closed(target_uid)
    clients.reopen_clients(instances)
    write_state(counts=session_counts(), clients=clients.running_clients(),
                last_sync=now() if result else None)
    invalidate_state_cache()
    return result, detail


def do_sync():
    uid = current_uid()
    if not uid:
        return False, "读不到当前登录 uid"
    write_state(status=config.SWITCHING_STATUS,
                last_action="manual session sync", last_error="")
    ok, detail = switch_with_clients_closed(uid)
    write_state(status="watching" if ok else "error",
                last_error="" if ok else detail,
                last_action="manual session sync")
    invalidate_state_cache()
    return ok, detail


def external_state(use_cache=True):
    """聚合面板状态；短 TTL 缓存抑制 3 秒轮询带来的重复子进程/数据库开销。"""
    def produce():
        state = read_state()
        uid = current_uid()
        counts = session_counts()
        entries, active = account_entries()
        balances = state.get("balances") or {}
        rows = []
        for entry in entries:
            uid_full = entry.get("uid", "")
            balance = balances.get(entry["name"], {})
            expiries = sorted(balance.get("expiries") or [], key=lambda item: item.get("at", ""))
            credential = signin.credential_path(entry)
            rows.append({
                "name": entry["name"], "uid": uid_full,
                "short_uid": uid_full[:8] + "…" if uid_full else "",
                "active": entry["name"] == active,
                "current": uid_full == uid,
                "sessions": counts.get(uid_full, 0),
                "expires_at": signin._credential_expiry_text(credential),
                "credential_ok": os.path.exists(credential),
                "balance": balance.get("balance"),
                "next_expiry": expiries[0] if expiries else None,
                "balance_error": balance.get("error", ""),
            })
        state.update({
            "current_uid": uid,
            "current_account": account_name_for(uid, entries),
            "clients": clients.running_clients(),
            "accounts": rows,
        })
        return state

    return _state_cache.get(produce) if use_cache else produce()

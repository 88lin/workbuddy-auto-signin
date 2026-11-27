"""状态持久化、日志、单实例锁与跨线程命令队列。"""
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import config

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX 分支
    import fcntl
    msvcrt = None


def now():
    return datetime.now().strftime(config.STATE_TIME_FORMAT)


def log(message):
    """日志写入 + 控制台回显。两者都不允许因 I/O 失败中断业务。"""
    line = "%s  %s" % (now(), message)
    try:
        with open(config.LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    try:
        print(line, flush=True)
    except (OSError, ValueError):  # pythonw 无 stdout，或管道已断开
        pass


def read_state():
    """读状态；损坏时明确告警而不是静默当作空状态。

    静默返回 {} 会丢掉 last_switch_at / last_backup 等风控与恢复信息。
    """
    try:
        text = config.STATE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log("WARNING: state file unreadable: %s" % exc)
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        log("WARNING: state file corrupt (%s); treating as empty" % exc)
        return {}
    if not isinstance(data, dict):
        log("WARNING: state file is not a JSON object; treating as empty")
        return {}
    return data


def write_state(**updates):
    """原子合并写入状态。失败只记日志，不让看板 API 500。"""
    state = read_state()
    state.update(updates)
    state["updated_at"] = now()
    tmp = config.STATE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, config.STATE_FILE)
    except OSError as exc:
        log("write_state failed: %s" % exc)


class SingleInstance:
    """OS 级单实例锁；崩溃自动释放。支持 with 语法，杜绝忘记释放。"""

    def __init__(self, path):
        self.path = Path(path)
        self.fh = None

    def acquire(self):
        try:
            self.fh = open(self.path, "a+")
            self.fh.seek(0)
            if msvcrt:
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if self.fh is not None:
                self.fh.close()
            self.fh = None
            return False
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return True

    def release(self):
        if self.fh is None:
            return
        try:
            self.fh.seek(0)
            if msvcrt:
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.fh.close()
            self.fh = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("another hub instance is already running")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


def _acquire_handle(handle):
    if msvcrt:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_handle(handle):
    if msvcrt:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(path, timeout=10.0):
    """短临界区文件锁：open() 与加锁都带退避，避免 Windows 并发 PermissionError。"""
    handle = None
    deadline = datetime.now().timestamp() + timeout
    while handle is None:
        try:
            handle = open(path, "a+b")
        except OSError:
            if datetime.now().timestamp() >= deadline:
                raise RuntimeError("cannot open lock file: %s" % path)
            threading.Event().wait(0.05)
    try:
        while True:
            try:
                _acquire_handle(handle)
                break
            except OSError:
                if datetime.now().timestamp() >= deadline:
                    raise RuntimeError("cannot acquire lock file: %s" % path)
                threading.Event().wait(0.05)
        try:
            yield
        finally:
            try:
                _release_handle(handle)
            except OSError:
                pass
    finally:
        handle.close()


COMMAND_ORDER = ("balance", "switch", "sync")


class CommandQueue:
    """HTTP 线程 -> 守护线程的命令队列。

    同类命令合并（新值覆盖旧值），跨类命令按固定优先级消费。
    put / drain 全程持锁，消除原模块级 dict 的竞态与静默覆盖。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}

    def put(self, action, payload=None):
        with self._lock:
            self._pending[action] = payload

    def drain(self):
        with self._lock:
            items = [(action, self._pending.pop(action))
                     for action in COMMAND_ORDER if action in self._pending]
        return items

    def clear(self):
        with self._lock:
            self._pending.clear()

    def snapshot(self):
        with self._lock:
            return dict(self._pending)


# 全局命令总线：由 web 写入、daemon 消费；测试可注入独立实例。
commands = CommandQueue()


class SnapshotCache:
    """极简 TTL 快照：避免每 3 秒面板轮询重复跑子进程 / sqlite / 重试。"""

    def __init__(self, ttl=None):
        self.ttl = config.STATE_TTL_SECONDS if ttl is None else ttl
        self._value = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def get(self, producer):
        import time
        now_ts = time.monotonic()
        with self._lock:
            if self._value is not None and now_ts < self._expires:
                return self._value
        value = producer()
        with self._lock:
            self._value = value
            self._expires = time.monotonic() + self.ttl
        return value

    def invalidate(self):
        with self._lock:
            self._value = None
            self._expires = 0.0

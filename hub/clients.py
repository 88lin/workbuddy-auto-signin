"""客户端进程管理：发现、关闭、重开。

关键修正：原实现分别调用 tasklist（名字）和 PowerShell（路径）再 zip 配对，
两者顺序无保证，双客户端并存时会重开错误的 exe。这里用一次 WMI 查询返回
(name, path) 对，从源头消除错配。
"""
import subprocess
import time
from pathlib import Path

from . import config
from .state import log


def _run(args, timeout=15):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          creationflags=config.NO_WINDOW)


def running_clients():
    """返回当前运行的客户端名列表；探测失败返回 None 以区别于'没有客户端'。"""
    try:
        out = _run(["tasklist", "/FO", "CSV", "/NH"]).stdout
    except Exception:
        return None
    found = set()
    for line in out.splitlines():
        low = line.lower()
        for name in config.CLIENT_NAMES:
            if low.startswith('"%s"' % name.lower()):
                found.add(name)
    return sorted(found)


def client_instances():
    """一次查询返回 [(name, executable_path)]，保证名字与路径一一对应。"""
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'WorkBuddy.exe' -or $_.Name -eq 'WorkBuddyAI.exe' } | "
        "ForEach-Object { \"$($_.Name)|$($_.ExecutablePath)\" }"
    )
    try:
        out = _run(["powershell", "-NoProfile", "-Command", ps], timeout=20).stdout
    except Exception:
        return []
    instances = []
    for line in out.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        name, path = line.split("|", 1)
        name, path = name.strip(), path.strip()
        if name in config.CLIENT_NAMES and path.lower().endswith(".exe"):
            instances.append((name, path))
    return instances


def close_clients():
    """冷关闭客户端。返回 (是否全部关闭, [(name, path)])。"""
    instances = client_instances()
    names = sorted({name for name, _ in instances}) or (running_clients() or [])
    if not names:
        return True, []

    for name in names:
        try:
            _run(["taskkill", "/IM", name])
        except Exception:
            pass

    deadline = time.time() + 20
    while time.time() < deadline:
        still = running_clients()
        if still == []:
            return True, instances
        if still is None:
            break
        time.sleep(1)

    for name in names:
        try:
            _run(["taskkill", "/IM", name, "/F"])
        except Exception:
            pass
    time.sleep(2)
    return running_clients() == [], instances


def reopen_clients(instances):
    """只重开切换前确实在跑的版本，不凭空多开客户端。"""
    for name, path in instances:
        if not path or not Path(path).exists():
            log("client exe missing, cannot reopen %s: %s" % (name, path))
            continue
        try:
            subprocess.Popen([path], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=config.NO_WINDOW)
        except OSError as exc:
            log("cannot reopen %s (%s): %s" % (name, path, exc))

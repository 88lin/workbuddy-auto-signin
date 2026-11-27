"""命令行入口与可移植自检。

self-test 只验证纯逻辑与锁语义，不依赖真实凭据 / migrate.py / 客户端，
因此可以在 CI 与任意机器上执行。
"""
import json
import sys
from http.client import HTTPConnection

from . import billing, config, service
from .state import SingleInstance, log

USAGE = """WorkBuddy 统一控制台

  python hub.py               启动看守 + 面板（常驻）
  python hub.py status        打印状态 JSON
  python hub.py switch <名>   切换账号并搬会话（经守护进程队列）
  python hub.py sync          只同步会话到当前登录账号
  python hub.py balance       强制刷新余额
  python hub.py self-test     逻辑自检
"""


def api_call(action, name=None):
    try:
        connection = HTTPConnection("127.0.0.1", config.PORT, timeout=5)
        body = ("name=" + name) if name else ""
        connection.request("POST", "/api/" + action, body=body,
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        response = connection.getresponse()
        output = response.read().decode("utf-8", "replace")
        print(output)
        return response.status == 200
    except Exception as exc:
        print("hub daemon not reachable on 127.0.0.1:%d: %s" % (config.PORT, exc))
        return False


def self_test():
    counts = {"a": 10, "b": 0, "c": 3}
    assert [uid for uid, number in sorted(counts.items(), key=lambda kv: -kv[1])
            if uid != "a" and number > 0] == ["c"]

    assert billing._package_remain({
        "CycleCapacitySize": 100, "CycleCapacityRemain": 120,
        "CycleCapacityUsed": 10}) == 90
    assert billing._package_remain({"CapacityRemain": 42}) == 42
    assert billing._resource_accounts(
        {"Response": {"Data": {"Accounts": [{"CapacityRemain": 1}]}}}) == [{"CapacityRemain": 1}]
    assert billing.parse_balance_response({"Accounts": [
        {"CapacityRemain": 7, "CycleEndTime": "2026-12-01 00:00:00"}]})["balance"] == 7
    assert "error" in billing.parse_balance_response(["not", "a", "dict"])
    assert (config.ASSETS / "js" / "app.js").exists(), "dashboard assets missing"

    lock_path = config.LOCK.with_name("hub-selftest.lock")
    with SingleInstance(lock_path):  # with 保证释放，杜绝遗留锁文件
        pass
    with SingleInstance(lock_path):
        pass

    log("self-test OK")
    print("self-test OK")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("panel", "daemon"):
        from .daemon import daemon
        daemon()
        return
    if args[0] == "self-test":
        self_test()
        return
    if args[0] == "status":
        print(json.dumps(service.external_state(use_cache=False), ensure_ascii=False, indent=2))
        return
    if args[0] in ("switch", "sync", "balance"):
        if args[0] == "switch" and len(args) < 2:
            print("usage: hub.py switch <account-name>")
            sys.exit(2)
        ok = api_call(args[0], args[1] if args[0] == "switch" else None)
        sys.exit(0 if ok else 1)
    print(USAGE)

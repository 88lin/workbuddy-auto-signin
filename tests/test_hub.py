"""hub 模块单元测试：只用临时目录与 mock，不碰真实凭据、客户端或网络。

覆盖本轮工程化修掉的真实缺陷：
  - registry 并发 open 的 PermissionError 重试（signin 侧）
  - 余额响应非 dict 时的 AttributeError
  - 账号注册表重复读取
  - 命令队列无锁竞态 / 同类请求静默覆盖
  - 单实例锁泄漏、状态文件损坏静默吞错
  - 静态资源路径穿越、HTTP body 未限制
"""
import json
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import signin

from hub import billing, cli, clients, config, service, state, web
from hub.state import commands


class HubTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.targets = {
            "STATE_FILE": self.root / "hub-state.json",
            "LOG": self.root / "hub.log",
            "LOCK": self.root / "hub.lock",
            "SNAPSHOT": self.root / "snapshot.json",
            "DB_PATH": self.root / "workbuddy.db",
            "DASHBOARD": self.root / "dashboard.html",
            "ASSETS": self.root / "assets",
            "BACKUPS": self.root / "backups",
        }
        self.patchers = [patch.object(config, key, value)
                         for key, value in self.targets.items()]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)


class BillingTests(unittest.TestCase):
    def test_cycle_remain_is_clamped_and_corrected_by_used(self):
        self.assertEqual(billing._package_remain({
            "CycleCapacitySize": 100, "CycleCapacityRemain": 120,
            "CycleCapacityUsed": 10}), 90)
        # used 超过推算值时按 used 反修正：size=100、remain=80 推出 used=20，
        # 而实际 used=50，因此 remain 应被修正为 50。
        self.assertEqual(billing._package_remain({
            "CycleCapacitySize": 100, "CycleCapacityRemain": 80,
            "CycleCapacityUsed": 50}), 50)

    def test_missing_cycle_falls_back_to_capacity_remain(self):
        self.assertEqual(billing._package_remain({"CapacityRemain": 42}), 42)

    def test_non_dict_response_returns_error_instead_of_raising(self):
        # 原实现先 biz=... if isinstance 再 resp.get("Response")，list 响应会 AttributeError。
        for response in (["unexpected"], None, "text", 7):
            with self.subTest(response=response):
                result = billing.parse_balance_response(response)
                self.assertIn("error", result)
                self.assertNotIn("balance", result)

    def test_business_code_and_unknown_structure_are_errors(self):
        self.assertIn("error", billing.parse_balance_response({"code": 500}))
        self.assertIn("error", billing.parse_balance_response({"weird": "shape"}))

    def test_negative_remain_is_clamped_to_zero_and_expiry_skipped(self):
        parsed = billing.parse_balance_response({"Accounts": [
            {"CapacityRemain": -3, "CycleEndTime": "2026-12-01 00:00:00"}]})
        self.assertEqual(parsed["balance"], 0)
        self.assertEqual(parsed["expiries"], [])

    def test_multi_layer_envelope_and_expiry_ordering(self):
        parsed = billing.parse_balance_response({"Response": {"Data": {"Accounts": [
            {"CapacityRemain": 5, "CycleEndTime": "2026-12-02 00:00:00"},
            {"CapacityRemain": 2, "CycleEndTime": "2026-12-01 00:00:00"},
        ]}}})
        self.assertEqual(parsed["balance"], 7)
        self.assertEqual([e["at"] for e in parsed["expiries"]],
                         ["2026-12-02 00:00:00", "2026-12-01 00:00:00"])

    def test_fetch_balances_uses_injected_query_without_delay(self):
        entries = [{"name": "a"}, {"name": "b"}]
        result = billing.fetch_balances(
            entries, query=lambda entry: {"balance": len(entry["name"])}, delay=0)
        self.assertEqual(result, {"a": {"balance": 1}, "b": {"balance": 1}})


class StateTests(HubTestCase):
    def test_corrupt_state_is_reported_not_silently_ignored(self):
        config.STATE_FILE.write_text("{broken", encoding="utf-8")
        with patch.object(state, "log") as recorded:
            self.assertEqual(state.read_state(), {})
        recorded.assert_called_once()
        self.assertIn("corrupt", recorded.call_args.args[0])

    def test_non_object_state_is_rejected(self):
        config.STATE_FILE.write_text("[1,2,3]", encoding="utf-8")
        self.assertEqual(state.read_state(), {})

    def test_write_state_merges_atomically(self):
        state.write_state(a=1)
        state.write_state(b=2)
        data = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
        self.assertEqual((data["a"], data["b"]), (1, 2))
        self.assertIn("updated_at", data)
        self.assertFalse(config.STATE_FILE.with_suffix(".tmp").exists())

    def test_single_instance_is_released_by_context_manager(self):
        with state.SingleInstance(config.LOCK):
            pass
        with state.SingleInstance(config.LOCK):
            pass

    def test_single_instance_rejects_concurrent_holder(self):
        with state.SingleInstance(config.LOCK):
            self.assertFalse(state.SingleInstance(config.LOCK).acquire())

    def test_command_queue_coalesces_and_keeps_priority(self):
        queue = state.CommandQueue()
        queue.put("switch", "a")
        queue.put("switch", "b")  # 同类新值覆盖旧值
        queue.put("sync", True)
        queue.put("balance", True)
        self.assertEqual(queue.drain(),
                         [("balance", True), ("switch", "b"), ("sync", True)])
        self.assertEqual(queue.drain(), [])

    def test_command_queue_is_thread_safe(self):
        queue = state.CommandQueue()

        def writer(index):
            queue.put("sync", index)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(len(queue.drain()), 1)  # 同类合并为一条

    def test_snapshot_cache_reuses_then_recomputes(self):
        cache = state.SnapshotCache(ttl=0.05)
        calls = []

        def producer():
            calls.append(1)
            return len(calls)

        self.assertEqual(cache.get(producer), 1)
        self.assertEqual(cache.get(producer), 1)
        self.assertEqual(len(calls), 1)
        time.sleep(0.06)
        self.assertEqual(cache.get(producer), 2)
        cache.invalidate()
        self.assertEqual(cache.get(producer), 3)


class ClientTests(HubTestCase):
    def test_running_clients_parses_tasklist_csv(self):
        output = ('"WorkBuddy.exe","1234","Console"\r\n'
                  '"WorkBuddyAI.exe","5678","Console"\r\n'
                  '"unrelated.exe","9","Console"\r\n')
        with patch.object(clients, "_run", return_value=SimpleNamespace(stdout=output)):
            self.assertEqual(clients.running_clients(), ["WorkBuddy.exe", "WorkBuddyAI.exe"])

    def test_client_instances_keep_name_and_path_paired(self):
        # 回归原实现：名字来自 tasklist、路径来自另一条命令，zip 顺序无保证。
        output = ("WorkBuddy.exe|C:\\Program Files\\WB\\WorkBuddy.exe\r\n"
                  "WorkBuddyAI.exe|D:\\AI\\WorkBuddyAI.exe\r\n"
                  "garbage-line\r\n")
        with patch.object(clients, "_run", return_value=SimpleNamespace(stdout=output)):
            result = clients.client_instances()
        self.assertEqual(result, [
            ("WorkBuddy.exe", "C:\\Program Files\\WB\\WorkBuddy.exe"),
            ("WorkBuddyAI.exe", "D:\\AI\\WorkBuddyAI.exe"),
        ])

    def test_close_with_no_clients_does_not_spawn_commands(self):
        with patch.object(clients, "client_instances", return_value=[]), \
             patch.object(clients, "running_clients", return_value=[]), \
             patch.object(clients, "_run") as runner:
            self.assertEqual(clients.close_clients(), (True, []))
        runner.assert_not_called()

    def test_reopen_skips_missing_or_empty_paths(self):
        with patch.object(clients.subprocess, "Popen") as popen:
            clients.reopen_clients([("WorkBuddy.exe", ""),
                                    ("WorkBuddyAI.exe", str(Path("/nonexistent/app.exe")))])
        popen.assert_not_called()


class ServiceTests(HubTestCase):
    def test_account_entries_reads_registry_only_once(self):
        registry = {"accounts": [{"name": "a"}], "active": "a"}
        with patch.object(service.signin, "load_registry", return_value=registry) as loader:
            entries, active = service.account_entries()
        self.assertEqual(loader.call_count, 1)  # 原实现调用两次
        self.assertEqual((entries, active), (registry["accounts"], "a"))

    def test_switch_allowed_rejects_within_cooldown(self):
        with patch.object(service, "read_state", return_value={
                "last_switch_at": state.now()}):
            allowed, reason = service.switch_allowed()
        self.assertFalse(allowed)
        self.assertIn("180", reason)

    def test_switch_allowed_accepts_absent_or_corrupt_timestamp(self):
        for value in ({}, {"last_switch_at": "not-a-time"},
                      {"last_switch_at": "2020-01-01 00:00:00"}):
            with self.subTest(value=value), patch.object(service, "read_state", return_value=value):
                self.assertTrue(service.switch_allowed()[0])

    def test_external_state_uses_short_ttl_cache(self):
        service.invalidate_state_cache()
        with patch.object(service, "current_uid", return_value="u") as uid, \
             patch.object(service, "session_counts", return_value={}), \
             patch.object(service, "account_entries", return_value=([], None)), \
             patch.object(service.clients, "running_clients", return_value=[]):
            service.external_state()
            service.external_state()
        self.assertEqual(uid.call_count, 1)
        service.invalidate_state_cache()

    def test_account_name_for_uses_supplied_entries(self):
        entries = [{"name": "acc1", "uid": "u1"}]
        self.assertEqual(service.account_name_for("u1", entries), "acc1")
        self.assertEqual(service.account_name_for("unknown", entries), "")


class WebTests(HubTestCase):
    def setUp(self):
        super().setUp()
        self.server = web.create_server(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.port = self.server.server_address[1]
        commands.clear()
        self.addCleanup(commands.clear)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def _request(self, method, path, body=""):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body,
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        response = connection.getresponse()
        raw = response.read().decode("utf-8", "replace")
        connection.close()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw
        return response.status, payload

    def test_switch_name_validation(self):
        for bad in ("", "../evil", "a/b", "a\\b", "a\nb", "x" * 65):
            with self.subTest(bad=bad):
                status, body = self._request("POST", "/api/switch", "name=" + bad)
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])
        self.assertIsNone(commands.snapshot().get("switch"))

    def test_switch_enqueues_without_executing(self):
        status, body = self._request("POST", "/api/switch", "name=acc2")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(commands.snapshot()["switch"], "acc2")

    def test_oversized_body_is_rejected(self):
        status, body = self._request("POST", "/api/sync",
                                     "x=" + "y" * (config.HTTP_BODY_LIMIT + 1))
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_asset_path_traversal_is_forbidden(self):
        status, _ = self._request("GET", "/assets/dashboard/../../hub.py")
        self.assertEqual(status, 403)

    def test_pause_and_resume_update_state_and_invalidate_cache(self):
        with patch.object(service, "invalidate_state_cache") as invalidate:
            status, _ = self._request("POST", "/api/pause")
            self.assertEqual(status, 200)
            self.assertTrue(state.read_state()["paused"])
            status, _ = self._request("POST", "/api/resume")
            self.assertEqual(status, 200)
            self.assertFalse(state.read_state()["paused"])
        self.assertEqual(invalidate.call_count, 2)

    def test_unknown_route_returns_404(self):
        self.assertEqual(self._request("GET", "/does-not-exist")[0], 404)


class CliTests(HubTestCase):
    def test_self_test_releases_its_lock(self):
        lock = config.LOCK.with_name("hub-selftest.lock")
        with patch.object(config, "ASSETS", Path(signin.__file__).parent / "assets" / "dashboard"):
            cli.self_test()
        # 原实现第二次 acquire 后未 release：再次获取会失败。
        with state.SingleInstance(lock):
            pass

    def test_status_command_uses_uncached_state(self):
        with patch.object(service, "external_state", return_value={"status": "watching"}) as ext:
            cli.main(["status"])
        ext.assert_called_once_with(use_cache=False)


class RegistryConcurrencyTests(unittest.TestCase):
    """signin 锁的回归测试：open() 阶段抛 PermissionError 必须重试而非崩溃。"""

    def test_open_permission_error_is_retried(self):
        real_open = open
        attempts = []

        def flaky_open(path, mode, *args, **kwargs):
            attempts.append(path)
            if len(attempts) < 3:
                raise PermissionError(13, "Permission denied", path)
            return real_open(path, mode, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.dict("os.environ", {"WORKBUDDY_ACCOUNTS_DIR": str(root)}), \
                 patch("builtins.open", side_effect=flaky_open):
                with signin._registry_lock(timeout=2.0):
                    pass
        self.assertEqual(len(attempts), 3)

    def test_open_permission_error_times_out_with_accounts_error(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"WORKBUDDY_ACCOUNTS_DIR": temp}), \
                 patch("builtins.open", side_effect=PermissionError(13, "denied")), \
                 self.assertRaises(signin.AccountsError):
                with signin._registry_lock(timeout=0.15):
                    pass


if __name__ == "__main__":
    unittest.main()

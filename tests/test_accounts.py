"""多账号：账号清单、凭据快照、切换与轮转执行。

这些用例只操作临时目录里的假凭据，不联网、不碰真实客户端数据。
"""

import base64
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import signin


UID_A = "aaaa1111-1111-4111-8111-111111111111"
UID_B = "bbbb2222-2222-4222-8222-222222222222"
UID_LIVE = "cccc3333-3333-4333-8333-333333333333"


def credential(uid=UID_A, token="token-a",
               endpoint="https://example.com", expires_at=None):
    session = {"auth": {"accessToken": token, "endpoint": endpoint,
                        "domain": "www.workbuddy.ai"},
               "account": {"uid": uid, "enterpriseId": ""}}
    if expires_at is not None:
        session["auth"]["expiresAt"] = expires_at
    return session


def account_token(headers):
    """从请求头反查是哪个账号：测试凭据用不同的明文令牌。"""
    return headers["Authorization"].rsplit(" ", 1)[-1]


def envelope_token():
    """构造一份本地格式合法的 sym-v1 加密信封（不涉及真实密钥）。"""
    envelope = {"suite": 1, "keyId": "0123456789abcdef",
                "nonce": base64.b64encode(b"\x00" * 12).decode(),
                "authTag": base64.b64encode(b"\x01" * 16).decode(),
                "ciphertext": base64.b64encode(b"payload").decode()}
    return {"$wbEncrypted": 1,
            "envelope": base64.b64encode(json.dumps(envelope).encode()).decode()}


class AccountTestCase(unittest.TestCase):
    def setUp(self):
        signin._start_budget()
        signin._sensitive_values.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client_file = self.root / "workbuddy-desktop-ai.info"
        self.accounts_root = self.root / "accounts"
        self.environment = patch.dict(os.environ, {
            "WORKBUDDY_ACCOUNTS_DIR": str(self.accounts_root),
            "WORKBUDDY_AUTH_FILE": str(self.client_file),
            "WORKBUDDY_SIGNIN_LOG": str(self.root / "signin.log"),
            "WORKBUDDY_ENDPOINT": "",
            "WORKBUDDY_BUDGET_SECONDS": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.write_client(credential())

    def write_client(self, session):
        self.client_file.write_text(json.dumps(session), encoding="utf-8")

    def bind(self, name, session=None, **kwargs):
        if session is not None:
            self.write_client(session)
        return signin.bind_account(name, **kwargs)

    def bind_ab(self):
        self.bind("a", credential(uid=UID_A, token="token-a"))
        self.bind("b", credential(uid=UID_B, token="token-b"))


class AccountNameTests(AccountTestCase):
    def test_bind_rejects_names_that_are_not_plain_filenames(self):
        for name in ("", " ", "-x", ".", "..", "a/b", "a\\b", "a b", "a:b", "x" * 33):
            with self.subTest(name=name), self.assertRaises(signin.AccountsError):
                self.bind(name)
        self.assertFalse((self.accounts_root / "accounts.json").exists())

    def test_bind_accepts_chinese_and_punctuation_inside_name(self):
        self.bind("工作_1.主号")
        self.assertEqual(signin.find_account("工作_1.主号")["uid"], UID_A)


class BindTests(AccountTestCase):
    def test_bind_copies_credential_and_registers_account(self):
        entry = self.bind("work")
        self.assertEqual(entry["account"], "work")
        registry = signin.load_registry()
        self.assertEqual([a["name"] for a in registry["accounts"]], ["work"])
        self.assertEqual(registry["active"], "work")
        stored = signin.credential_path(registry["accounts"][0])
        self.assertEqual(Path(stored).read_bytes(), self.client_file.read_bytes())
        on_disk = Path(self.accounts_root / "accounts.json").read_text(encoding="utf-8")
        self.assertNotIn("token-a", on_disk)
        self.assertNotIn("token-a", json.dumps(entry))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(Path(stored).stat().st_mode) & 0o077, 0)

    def test_bind_from_explicit_file(self):
        other = self.root / "other.info"
        other.write_text(json.dumps(credential(uid=UID_LIVE, token="token-live")),
                         encoding="utf-8")
        entry = self.bind("other", source=str(other))
        self.assertEqual(entry["uid"], "cccc3333…")

    def test_bind_existing_name_needs_force_and_updates_snapshot(self):
        self.bind("work")
        self.write_client(credential(uid=UID_B, token="token-b2"))
        with self.assertRaises(signin.AccountsError):
            self.bind("work")
        self.bind("work", force=True)
        registry = signin.load_registry()
        self.assertEqual(len(registry["accounts"]), 1)
        self.assertEqual(registry["accounts"][0]["uid"], UID_B)
        stored = signin.credential_path(registry["accounts"][0])
        self.assertIn("token-b2", Path(stored).read_text(encoding="utf-8"))

    def test_same_identity_under_another_name_needs_force(self):
        self.bind("work")
        with self.assertRaises(signin.AccountsError):
            self.bind("home")
        self.bind("home", force=True)
        self.assertEqual(len(signin.load_registry()["accounts"]), 2)

    def test_bind_rejects_unusable_credentials(self):
        missing = self.root / "missing.info"
        cases = [
            (missing, None),
            (self.client_file, "{not json"),
            (self.client_file, json.dumps({"auth": {}, "account": {}})),
            (self.client_file, json.dumps(credential(token="bad token with space"))),
        ]
        for path, content in cases:
            with self.subTest(content=content):
                if content is not None:
                    path.write_text(content, encoding="utf-8")
                with self.assertRaises(signin.AccountsError):
                    signin.bind_account("work", source=str(path))


class BindTransactionTests(AccountTestCase):
    def assert_original_binding(self, registry_bytes, entry, snapshot_bytes):
        self.assertEqual(Path(signin._registry_path()).read_bytes(), registry_bytes)
        self.assertEqual(signin.find_account("work"), entry)
        self.assertEqual(Path(signin.credential_path(entry)).read_bytes(), snapshot_bytes)
        self.assertEqual(list(self.accounts_root.glob("*.info")),
                         [Path(signin.credential_path(entry))])

    def test_force_registry_failure_preserves_old_snapshot_and_manifest(self):
        self.bind("work", credential(uid=UID_A, token="token-a"))
        entry = signin.find_account("work")
        original = Path(signin.credential_path(entry)).read_bytes()
        registry_bytes = Path(signin._registry_path()).read_bytes()
        self.write_client(credential(uid=UID_B, token="token-b"))
        with patch.object(signin, "save_registry", side_effect=PermissionError("registry locked")):
            with self.assertRaises(OSError):
                signin.bind_account("work", force=True)
        self.assert_original_binding(registry_bytes, entry, original)

    @unittest.skipUnless(os.name == "nt", "Windows denies replacement of an open registry")
    def test_real_windows_registry_lock_preserves_old_snapshot(self):
        self.bind("work", credential(uid=UID_A, token="token-a"))
        entry = signin.find_account("work")
        original = Path(signin.credential_path(entry)).read_bytes()
        registry_bytes = Path(signin._registry_path()).read_bytes()
        self.write_client(credential(uid=UID_B, token="token-b"))
        with open(signin._registry_path(), "rb"):
            with self.assertRaises(OSError):
                signin.bind_account("work", force=True)
        self.assert_original_binding(registry_bytes, entry, original)

    def test_first_bind_registry_failure_removes_unpublished_snapshot(self):
        with patch.object(signin, "save_registry", side_effect=PermissionError("registry locked")):
            with self.assertRaises(OSError):
                self.bind("work")
        self.assertEqual(signin.load_registry()["accounts"], [])
        self.assertEqual(list(self.accounts_root.glob("*.info")), [])

    def test_snapshot_write_failure_preserves_existing_binding(self):
        self.bind("work", credential(uid=UID_A, token="token-a"))
        entry = signin.find_account("work")
        original = Path(signin.credential_path(entry)).read_bytes()
        registry_bytes = Path(signin._registry_path()).read_bytes()
        with patch.object(signin, "_atomic_write", side_effect=PermissionError("snapshot locked")):
            with self.assertRaises(OSError):
                self.bind("work", credential(uid=UID_B, token="token-b"), force=True)
        self.assert_original_binding(registry_bytes, entry, original)

    def test_successful_force_publishes_new_version_then_removes_old_snapshot(self):
        self.bind("work", credential(uid=UID_A, token="token-a"))
        original = signin.find_account("work")
        old_path = Path(signin.credential_path(original))
        self.bind("work", credential(uid=UID_B, token="token-b"), force=True)
        current = signin.find_account("work")
        self.assertNotEqual(current["credential"], original["credential"])
        self.assertEqual(current["uid"], UID_B)
        self.assertFalse(old_path.exists())
        self.assertEqual(Path(signin.credential_path(current)).read_bytes(), self.client_file.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows denies deletion of an open snapshot")
    def test_force_reports_old_snapshot_cleanup_failure_after_commit(self):
        self.bind("work", credential(uid=UID_A, token="token-a"))
        old = Path(signin.credential_path(signin.find_account("work")))
        self.write_client(credential(uid=UID_B, token="token-b"))
        with open(old, "rb"), patch.object(signin, "emit") as emit:
            self.assertEqual(signin.main(["bind", "work", "--force"]), 1)
        out = emit.call_args.args[0]
        self.assertEqual(out["result"], "BOUND")
        self.assertTrue(out["needs_attention"])
        self.assertEqual(Path(out["credential_cleanup_path"]), old)
        self.assertEqual(signin.find_account("work")["uid"], UID_B)
        self.assertTrue(old.exists())


class SwitchTests(AccountTestCase):
    def test_use_switches_active_account(self):
        self.bind_ab()
        self.assertEqual(signin.load_registry()["active"], "a")
        out = signin.use_account("b")
        self.assertEqual(out["report"], "当前账号已切换为「b」")
        self.assertEqual(signin.load_registry()["active"], "b")
        with self.assertRaises(signin.AccountsError):
            signin.use_account("missing")

    def test_unbind_removes_entry_and_snapshot(self):
        self.bind_ab()
        entry = signin.find_account("b")
        stored = signin.credential_path(entry)
        signin.unbind_account("b")
        self.assertFalse(Path(stored).exists())
        self.assertEqual(signin.load_registry()["active"], "a")
        self.assertEqual([a["name"] for a in signin.load_registry()["accounts"]], ["a"])

    def test_unbinding_active_account_hands_over_to_next(self):
        self.bind_ab()
        signin.use_account("b")
        signin.unbind_account("b")
        self.assertEqual(signin.load_registry()["active"], "a")

    def test_list_accounts_masks_uid_and_reports_file_state(self):
        self.bind_ab()
        out = signin.list_accounts()
        self.assertEqual(out["count"], 2)
        self.assertEqual([row["name"] for row in out["accounts"]], ["a", "b"])
        self.assertTrue(out["accounts"][0]["active"])
        self.assertNotIn(UID_A, json.dumps(out))
        self.assertIn("已绑定 2 个账号", out["report"])
        self.assertTrue(all(row["credential_exists"] for row in out["accounts"]))

    def test_order_is_oldest_run_first_and_active_leads_never_run(self):
        self.bind_ab()
        registry = signin.load_registry()
        registry["active"] = "b"
        registry["accounts"][0]["last_run_at"] = "2026-11-12T10:00:00+08:00"
        registry["accounts"][1]["last_run_at"] = "2026-11-12T09:00:00+08:00"
        signin.save_registry(registry)
        # 跑过的账号按上次运行时间从早到晚，当前账号不再无条件插队
        self.assertEqual([a["name"] for a in signin.order_accounts(signin.load_registry())],
                         ["b", "a"])

        registry = signin.load_registry()
        registry["active"] = "b"
        for account in registry["accounts"]:
            account["last_run_at"] = ""
        signin.save_registry(registry)
        # 都没跑过时，当前账号先跑
        self.assertEqual([a["name"] for a in signin.order_accounts(signin.load_registry())],
                         ["b", "a"])


class UnbindCleanupTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.bind("work", credential())
        self.entry = signin.find_account("work")
        self.snapshot = Path(signin.credential_path(self.entry))

    def test_unavailable_snapshot_keeps_binding_for_retry(self):
        replace = signin.os.replace

        def fail_stage(source, target):
            if Path(source) == self.snapshot:
                raise PermissionError("snapshot locked")
            return replace(source, target)

        with patch.object(signin.os, "replace", side_effect=fail_stage), \
                patch.object(signin, "emit") as emit:
            self.assertEqual(signin.main(["unbind", "work"]), 1)
        self.assertTrue(emit.call_args.args[0]["needs_attention"])
        self.assertEqual(signin.find_account("work"), self.entry)
        self.assertTrue(self.snapshot.exists())
        self.assertTrue(signin.unbind_account("work")["credential_removed"])

    @unittest.skipUnless(os.name == "nt", "Windows denies moving an open snapshot")
    def test_real_windows_snapshot_lock_can_be_retried_by_name(self):
        with open(self.snapshot, "rb"):
            with self.assertRaises(signin.AccountsError):
                signin.unbind_account("work")
        self.assertEqual(signin.find_account("work"), self.entry)
        self.assertTrue(self.snapshot.exists())
        self.assertTrue(signin.unbind_account("work")["credential_removed"])
        self.assertFalse(self.snapshot.exists())
        self.assertEqual(signin.load_registry()["accounts"], [])

    def test_registry_commit_failure_restores_staged_snapshot(self):
        original = self.snapshot.read_bytes()
        registry_bytes = Path(signin._registry_path()).read_bytes()
        with patch.object(signin, "save_registry", side_effect=PermissionError("registry locked")):
            with self.assertRaises(OSError):
                signin.unbind_account("work")
        self.assertEqual(Path(signin._registry_path()).read_bytes(), registry_bytes)
        self.assertEqual(signin.find_account("work"), self.entry)
        self.assertEqual(self.snapshot.read_bytes(), original)
        self.assertEqual(list(self.accounts_root.glob("*.info")), [self.snapshot])

    @unittest.skipUnless(os.name == "nt", "Windows denies replacement of an open registry")
    def test_real_windows_registry_lock_restores_snapshot_on_failed_unbind(self):
        original = self.snapshot.read_bytes()
        with open(signin._registry_path(), "rb"):
            with self.assertRaises(OSError):
                signin.unbind_account("work")
        self.assertEqual(signin.find_account("work"), self.entry)
        self.assertEqual(self.snapshot.read_bytes(), original)
        self.assertEqual(list(self.accounts_root.glob("*.info")), [self.snapshot])

    def test_process_interruption_after_staging_keeps_binding_usable_and_retryable(self):
        child = (
            "import os, signin\n"
            "with signin._registry_lock():\n"
            " entry = signin.find_account('work')\n"
            " os.replace(signin.credential_path(entry), signin._unbind_credential_path(entry))\n"
            " os._exit(3)\n"
        )
        result = subprocess.run([sys.executable, "-c", child], cwd=Path(signin.__file__).parent,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse(self.snapshot.exists())
        self.assertTrue(signin.list_accounts()["accounts"][0]["credential_exists"])
        seen = []

        def daily(headers, endpoint):
            seen.append(account_token(headers))
            return 0, {"result": "ALREADY", "report": "ok"}, True

        with patch.object(signin, "run_daily", side_effect=daily), patch.object(signin, "emit"):
            self.assertEqual(signin._run("auto"), 0)
        self.assertEqual(seen, ["token-a"])
        self.assertTrue(signin.unbind_account("work")["credential_removed"])
        self.assertEqual(signin.load_registry()["accounts"], [])
        self.assertEqual(list(self.accounts_root.glob("*.info")), [])

    def test_final_cleanup_failure_reports_residual_path(self):
        unlink = signin.os.unlink

        def fail_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(".unbind-"):
                raise PermissionError("cleanup locked")
            return unlink(path, *args, **kwargs)

        with patch.object(signin.os, "unlink", side_effect=fail_cleanup), \
                patch.object(signin, "emit") as emit:
            self.assertEqual(signin.main(["unbind", "work"]), 1)
        out = emit.call_args.args[0]
        self.assertEqual(out["result"], "UNBOUND")
        self.assertTrue(out["needs_attention"])
        self.assertFalse(out["credential_removed"])
        residual = Path(out["credential_cleanup_path"])
        self.assertTrue(residual.exists())
        self.assertIn(str(residual), out["report"])
        self.assertEqual(signin.load_registry()["accounts"], [])

    def test_already_missing_snapshot_can_be_unbound(self):
        self.snapshot.unlink()
        out = signin.unbind_account("work")
        self.assertFalse(out["credential_removed"])
        self.assertFalse(out.get("needs_attention"))
        self.assertEqual(signin.load_registry()["accounts"], [])


class RegistryValidationTests(AccountTestCase):
    def test_missing_registry_is_empty_not_an_error(self):
        self.assertEqual(signin.load_registry()["accounts"], [])

    def test_corrupt_registry_is_reported_and_daily_run_falls_back(self):
        self.accounts_root.mkdir(parents=True)
        (self.accounts_root / "accounts.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(signin.AccountsError):
            signin.load_registry()
        with patch.object(signin, "run_daily",
                          return_value=(0, {"result": "ALREADY", "report": "今日已签过"}, True)), \
                patch.object(signin, "emit") as emit:
            self.assertEqual(signin._run("silent"), 0)
        out = emit.call_args.args[0]
        self.assertTrue(out["account_warnings"])
        self.assertIn("账号清单读取失败", out["account_warnings"][0])

    def test_registry_rejects_path_traversal_credential(self):
        self.accounts_root.mkdir(parents=True, exist_ok=True)
        for credential_name in ("../escape.info", "C:evil.info", "sub/x.info", ".", ".."):
            with self.subTest(credential=credential_name):
                (self.accounts_root / "accounts.json").write_text(json.dumps({
                    "version": 1, "active": "a",
                    "accounts": [{"name": "a", "credential": credential_name,
                                  "uid": "uid", "endpoint": "", "bound_at": "",
                                  "last_run_at": "", "last_result": "", "last_code": 0}],
                }), encoding="utf-8")
                with self.assertRaises(signin.AccountsError):
                    signin.load_registry()

    def test_registry_version_and_duplicates_are_rejected(self):
        self.accounts_root.mkdir(parents=True, exist_ok=True)
        entry = {"name": "a", "credential": "a.info", "uid": "uid", "endpoint": "",
                 "bound_at": "", "last_run_at": "", "last_result": "", "last_code": 0}
        for payload in ({"version": 2, "accounts": [entry]},
                        {"version": 1, "accounts": [entry, dict(entry)]}):
            with self.subTest(payload=payload):
                (self.accounts_root / "accounts.json").write_text(
                    json.dumps(payload), encoding="utf-8")
                with self.assertRaises(signin.AccountsError):
                    signin.load_registry()


class RotationTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.bind_ab()
        self.calls = []

    def fake_daily(self, result="CLAIMED", code=0, quiet=True):
        def run(headers, endpoint):
            token = account_token(headers)
            self.calls.append((token, endpoint))
            return code, {"result": result,
                          "report": "账号 %s %s" % (token, result),
                          "credits_gained": 100}, quiet
        return run

    def run_action(self, action="auto", account=None):
        emitted = []
        with patch.object(signin, "emit", side_effect=lambda out, step: emitted.append(out)):
            code = signin._run(action, account)
        return code, emitted

    def test_all_bound_accounts_run_once_each_active_first(self):
        signin.use_account("b")
        with patch.object(signin, "run_daily", side_effect=self.fake_daily()):
            code, emitted = self.run_action()
        self.assertEqual(code, 0)
        self.assertEqual([token for token, _ in self.calls], ["token-b", "token-a"])
        out = emitted[-1]
        self.assertEqual(out["result"], "ALL")
        self.assertEqual(out["total"], 2)
        self.assertEqual([row["account"] for row in out["accounts"]], ["b", "a"])
        self.assertEqual(out["credits_gained"], 200)
        self.assertIn("[b]", out["report"])

    def test_failure_in_one_account_does_not_stop_the_next(self):
        def run(headers, endpoint):
            token = account_token(headers)
            self.calls.append((token, endpoint))
            if token == "token-b":
                return 1, signin._auth_failure(401), False
            return 0, {"result": "CLAIMED", "report": "第二条成功",
                       "credits_gained": 100}, False

        with patch.object(signin, "run_daily", side_effect=run):
            code, emitted = self.run_action()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.calls), 2)
        out = emitted[-1]
        rows = {row["account"]: row["result"] for row in out["accounts"]}
        self.assertEqual(rows, {"a": "CLAIMED", "b": "AUTH_REJECTED"})
        self.assertTrue(out["needs_attention"])

    def test_unexpected_exception_isolated_to_one_account(self):
        def run(headers, endpoint):
            token = account_token(headers)
            self.calls.append((token, endpoint))
            if token == "token-b":
                raise RuntimeError("boom")
            return 0, {"result": "CLAIMED", "report": "ok", "credits_gained": 0}, False

        with patch.object(signin, "run_daily", side_effect=run):
            code, emitted = self.run_action()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.calls), 2)
        rows = {row["account"]: row["result"] for row in emitted[-1]["accounts"]}
        self.assertEqual(rows, {"a": "CLAIMED", "b": "ERROR"})

    def test_explicit_account_runs_only_that_account(self):
        with patch.object(signin, "run_daily", side_effect=self.fake_daily()) as daily:
            code, emitted = self.run_action(account="a")
        self.assertEqual(code, 0)
        daily.assert_called_once()
        self.assertEqual([token for token, _ in self.calls], ["token-a"])
        self.assertEqual(emitted[-1]["result"], "CLAIMED")
        self.assertEqual(emitted[-1]["account"], "a")

    def test_current_account_escape_hatch_ignores_registry(self):
        self.write_client(credential(uid=UID_LIVE, token="token-live"))
        with patch.object(signin, "run_daily", side_effect=self.fake_daily()) as daily:
            self.run_action(account="current")
        daily.assert_called_once()
        self.assertEqual([token for token, _ in self.calls], ["token-live"])

    def test_unknown_account_is_reported_without_running(self):
        emitted = []
        with patch.object(signin, "emit",
                          side_effect=lambda out, step: emitted.append(out)), \
                patch.object(signin, "run_daily") as daily:
            self.assertEqual(signin._run("auto", "missing"), 1)
        daily.assert_not_called()
        self.assertEqual(emitted[-1]["result"], "ERROR")

    def test_expired_snapshot_skips_network_and_asks_for_rebind(self):
        past = (time.time() - 3600) * 1000
        old = self.root / "expired.info"
        old.write_text(json.dumps(credential(uid=UID_LIVE, token="token-old",
                                             expires_at=past)), encoding="utf-8")
        signin.bind_account("old", source=str(old))
        with patch.object(signin, "run_daily", side_effect=self.fake_daily()):
            code, emitted = self.run_action()
        self.assertEqual(code, 1)
        self.assertEqual([token for token, _ in self.calls], ["token-a", "token-b"])
        row = next(r for r in emitted[-1]["accounts"] if r["account"] == "old")
        self.assertEqual(row["result"], "EXPIRED")
        self.assertIn("bind old --force", row["report"])

    def test_missing_snapshot_is_reported_per_account(self):
        entry = signin.find_account("b")
        Path(signin.credential_path(entry)).unlink()
        with patch.object(signin, "run_daily", side_effect=self.fake_daily()):
            code, emitted = self.run_action()
        self.assertEqual(code, 1)
        row = next(r for r in emitted[-1]["accounts"] if r["account"] == "b")
        self.assertEqual(row["result"], "MISSING")

    def test_same_identity_bound_twice_runs_once(self):
        self.bind("a-copy", force=True)
        with patch.object(signin, "run_daily", side_effect=self.fake_daily()) as daily:
            code, emitted = self.run_action()
        self.assertEqual(code, 0)
        self.assertEqual(daily.call_count, 2)
        self.assertEqual(emitted[-1]["account_warnings"][0].count("同一个登录身份"), 1)

    def test_short_budget_defers_remaining_accounts_without_failing(self):
        with patch.object(signin, "run_daily", side_effect=self.fake_daily()) as daily, \
                patch.object(signin, "_budget_left", return_value=signin.ACCOUNT_MIN_SLICE - 1):
            code, emitted = self.run_action()
        self.assertEqual(code, 0)
        daily.assert_not_called()
        self.assertEqual([row["result"] for row in emitted[-1]["accounts"]],
                         ["DEFERRED", "DEFERRED"])

    def test_budget_slice_is_restored_between_accounts(self):
        seen = []

        def run(headers, endpoint):
            seen.append(signin._budget_left())
            return 0, {"result": "ALREADY", "report": "ok"}, True

        with patch.object(signin, "run_daily", side_effect=run):
            self.run_action()
        self.assertTrue(all(0 < left <= signin.DEFAULT_BUDGET_SECONDS for left in seen))

    def test_run_records_last_result_into_registry(self):
        with patch.object(signin, "run_daily", side_effect=self.fake_daily()):
            self.run_action()
        for entry in signin.load_registry()["accounts"]:
            self.assertEqual(entry["last_result"], "CLAIMED")
            self.assertTrue(entry["last_run_at"])

    def test_poll_logs_only_when_something_happened(self):
        with patch.object(signin, "run_daily", side_effect=self.fake_daily(result="ALREADY")):
            code, emitted = self.run_action("silent-poll")
        self.assertEqual(code, 0)
        self.assertEqual(emitted, [])

        with patch.object(signin, "run_daily", side_effect=self.fake_daily(quiet=False)):
            code, emitted = self.run_action("silent-poll")
        self.assertEqual(code, 0)
        self.assertEqual(emitted[-1]["trigger"], "poll")
        self.assertEqual(emitted[-1]["result"], "ALL")

    def test_single_account_keeps_plain_daily_output(self):
        signin.unbind_account("b")
        with patch.object(signin, "run_daily", side_effect=self.fake_daily()):
            code, emitted = self.run_action()
        self.assertEqual(code, 0)
        self.assertEqual(emitted[-1]["result"], "CLAIMED")
        self.assertEqual(emitted[-1]["account"], "a")

    def test_growth_action_rotates_without_checkin(self):
        def growth(headers, endpoint):
            return 0, {"result": "GROWTH", "report": "成长中心"}
        with patch.object(signin, "run_growth", side_effect=growth) as fake:
            code, emitted = self.run_action("growth")
        self.assertEqual(code, 0)
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(emitted[-1]["result"], "ALL")


class SnapshotIdentityTests(AccountTestCase):
    def test_case_variant_bind_requires_force(self):
        self.bind("work", credential(uid=UID_A, token="token-a"))
        self.write_client(credential(uid=UID_B, token="token-b"))
        with self.assertRaises(signin.AccountsError):
            self.bind("Work")
        self.assertEqual([a["name"] for a in signin.load_registry()["accounts"]], ["work"])

    def test_forced_case_variants_keep_independent_snapshots(self):
        self.bind("work", credential(uid=UID_A, token="token-a"))
        self.bind("Work", credential(uid=UID_B, token="token-b"), force=True)
        entries = {a["name"]: a for a in signin.load_registry()["accounts"]}
        self.assertNotEqual(entries["work"]["credential"], entries["Work"]["credential"])
        first = Path(signin.credential_path(entries["work"]))
        second = Path(signin.credential_path(entries["Work"]))
        self.assertNotEqual(os.path.normcase(str(first)), os.path.normcase(str(second)))
        self.assertIn("token-a", first.read_text(encoding="utf-8"))
        self.assertIn("token-b", second.read_text(encoding="utf-8"))

        signin.unbind_account("Work")
        self.assertTrue(first.exists(), "解绑 Work 不能删掉 work 的快照")
        self.assertFalse(second.exists())

    def test_unbind_keeps_snapshot_shared_with_legacy_entry(self):
        # 旧版本用账号名当文件名，历史清单里可能有两个条目指向同一份文件。
        self.accounts_root.mkdir(parents=True, exist_ok=True)
        shared = self.accounts_root / "shared.info"
        shared.write_text(json.dumps(credential()), encoding="utf-8")
        base = {"uid": UID_A, "endpoint": "https://example.com", "bound_at": "",
                "last_run_at": "", "last_result": "", "last_code": 0}
        registry = {"version": 1, "active": "a", "accounts": [
            dict(base, name="a", credential="shared.info"),
            dict(base, name="b", uid=UID_B, credential="shared.info")]}
        (self.accounts_root / "accounts.json").write_text(json.dumps(registry), encoding="utf-8")

        out = signin.unbind_account("a")
        self.assertFalse(out["credential_removed"])
        self.assertTrue(shared.exists(), "另一个条目还在引用这份快照，不能删")


class SnapshotSelectionTests(AccountTestCase):
    def test_usable_duplicate_snapshot_wins_over_expired_one(self):
        past = (time.time() - 3600) * 1000
        self.bind("old", credential(uid=UID_A, token="token-old", expires_at=past))
        self.bind("fresh", credential(uid=UID_A, token="token-fresh"), force=True)
        signin.use_account("old")

        seen = []

        def daily(headers, endpoint):
            seen.append(account_token(headers))
            return 0, {"result": "ALREADY", "report": "ok"}, True

        with patch.object(signin, "run_daily", side_effect=daily), patch.object(signin, "emit"):
            self.assertEqual(signin._run("auto"), 0)
        self.assertEqual(seen, ["token-fresh"])

    def test_all_duplicates_unusable_still_reports_the_error(self):
        past = (time.time() - 3600) * 1000
        self.bind("old", credential(uid=UID_A, token="token-old", expires_at=past))
        self.bind("fresh", credential(uid=UID_A, token="token-fresh", expires_at=past),
                  force=True)
        signin.use_account("old")

        emitted = []
        with patch.object(signin, "run_daily") as daily, \
                patch.object(signin, "emit", side_effect=lambda out, step: emitted.append(out)):
            self.assertEqual(signin._run("auto"), 1)
        daily.assert_not_called()
        self.assertEqual(emitted[-1]["result"], "EXPIRED")

    def test_parsable_but_invalid_snapshot_is_not_usable(self):
        self.bind("old", credential(uid=UID_A, token="token-old"))
        old_path = Path(signin.credential_path(signin.find_account("old")))
        self.bind("fresh", credential(uid=UID_A, token="token-fresh"), force=True)
        old_path.write_text("{}", encoding="utf-8")
        signin.use_account("old")

        seen = []

        def daily(headers, endpoint):
            seen.append(account_token(headers))
            return 0, {"result": "ALREADY", "report": "ok"}, True

        with patch.object(signin, "run_daily", side_effect=daily), patch.object(signin, "emit"):
            self.assertEqual(signin._run("auto"), 0)
        self.assertEqual(seen, ["token-fresh"])

    def test_invalid_token_format_snapshot_is_not_usable(self):
        self.bind("old", credential(uid=UID_A, token="token-old"))
        old_path = Path(signin.credential_path(signin.find_account("old")))
        self.bind("fresh", credential(uid=UID_A, token="token-fresh"), force=True)
        old_path.write_text(json.dumps(credential(uid=UID_A, token="bad token")),
                            encoding="utf-8")
        contexts, _ = signin.credential_plan()
        self.assertEqual([c[0] for c in contexts], ["fresh"])

    def test_encrypted_envelope_snapshot_counts_as_usable(self):
        self.bind("old", credential(uid=UID_A, token="token-old"))
        old_path = Path(signin.credential_path(signin.find_account("old")))
        self.bind("fresh", credential(uid=UID_A, token=envelope_token()), force=True)
        old_path.write_text("{}", encoding="utf-8")
        contexts, _ = signin.credential_plan()
        self.assertEqual([c[0] for c in contexts], ["fresh"])
        self.assertTrue(contexts[0][1], "加密信封本地格式合法，应被选中（无需解密）")

    def test_uid_mismatch_snapshot_is_not_usable(self):
        self.accounts_root.mkdir(parents=True, exist_ok=True)
        (self.accounts_root / "a.info").write_text(
            json.dumps(credential(uid=UID_B, token="token-b")), encoding="utf-8")
        (self.accounts_root / "b.info").write_text(
            json.dumps(credential(uid=UID_A, token="token-a")), encoding="utf-8")
        base = {"endpoint": "https://example.com", "bound_at": "",
                "last_run_at": "", "last_result": "", "last_code": 0}
        signin.save_registry({"version": 1, "active": "a", "accounts": [
            dict(base, name="a", uid=UID_A, credential="a.info"),
            dict(base, name="b", uid=UID_A, credential="b.info")]})
        contexts, _ = signin.credential_plan()
        self.assertEqual([c[0] for c in contexts], ["b"])

    def test_all_parsable_but_invalid_snapshots_still_report_error(self):
        self.bind("old", credential(uid=UID_A, token="token-old"))
        old_path = Path(signin.credential_path(signin.find_account("old")))
        self.bind("fresh", credential(uid=UID_A, token="token-fresh"), force=True)
        fresh_path = Path(signin.credential_path(signin.find_account("fresh")))
        old_path.write_text("{}", encoding="utf-8")
        fresh_path.write_text("{}", encoding="utf-8")
        signin.use_account("old")

        emitted = []
        with patch.object(signin, "run_daily") as daily, \
                patch.object(signin, "emit", side_effect=lambda out, step: emitted.append(out)):
            self.assertEqual(signin._run("auto"), 1)
        daily.assert_not_called()
        self.assertIn(emitted[-1]["result"], ("NO_SESSION", "AUTH_ERROR"))


class RegistryLockTests(AccountTestCase):
    def test_lock_is_exclusive_across_threads(self):
        acquired = []

        def worker():
            with signin._registry_lock(timeout=5):
                acquired.append(True)

        with signin._registry_lock():
            thread = threading.Thread(target=worker)
            thread.start()
            time.sleep(0.2)
            self.assertEqual(acquired, [])
        thread.join(5)
        self.assertEqual(acquired, [True])

    def test_lock_timeout_raises_accounts_error(self):
        with signin._registry_lock():
            with self.assertRaises(signin.AccountsError):
                with signin._registry_lock(timeout=0.2):
                    pass

    def test_status_update_does_not_resurrect_unbound_entry(self):
        self.bind_ab()
        expected = signin.find_account("a")
        with signin._registry_lock():
            thread = threading.Thread(target=signin._record_account_run,
                                      args=("a", {"result": "ALREADY"}, 0, expected))
            thread.start()
            time.sleep(0.2)
            self.assertTrue(thread.is_alive(), "记录状态也要排队等锁")
            # 模拟另一个进程在这期间解绑了 b
            registry = signin.load_registry()
            registry["accounts"] = [x for x in registry["accounts"] if x["name"] != "b"]
            signin.save_registry(registry)
        thread.join(5)
        self.assertEqual([a["name"] for a in signin.load_registry()["accounts"]], ["a"])


class BoundIdentityExecutionTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.bind_ab()

    def replace_a_with_b(self, token="token-b"):
        path = Path(signin.credential_path(signin.find_account("a")))
        path.write_text(json.dumps(credential(uid=UID_B, token=token)), encoding="utf-8")

    def test_mismatched_fallback_is_rejected_without_blocking_b(self):
        self.replace_a_with_b()
        seen = []

        def daily(headers, endpoint):
            seen.append(account_token(headers))
            return 0, {"result": "ALREADY", "report": "ok"}, True

        with patch.object(signin, "run_daily", side_effect=daily), \
                patch.object(signin, "emit") as emit:
            self.assertEqual(signin._run("auto"), 1)
        self.assertEqual(seen, ["token-b"])
        rows = {row["account"]: row for row in emit.call_args.args[0]["accounts"]}
        self.assertEqual(rows["a"]["result"], "AUTH_ERROR")
        self.assertEqual(rows["b"]["result"], "ALREADY")
        self.assertEqual(signin.find_account("a")["last_result"], "AUTH_ERROR")

    def test_explicit_mismatch_is_rejected_before_decryption(self):
        self.replace_a_with_b(token=envelope_token())
        with patch.object(signin, "resolve_session") as resolve, \
                patch.object(signin, "run_daily") as daily, \
                patch.object(signin, "emit") as emit:
            self.assertEqual(signin._run("auto", "a"), 1)
        resolve.assert_not_called()
        daily.assert_not_called()
        self.assertEqual(emit.call_args.args[0]["reason"], "UID_MISMATCH")

    def test_doctor_mismatch_is_rejected_before_runtime_probe(self):
        self.replace_a_with_b(token=envelope_token())
        with patch.object(signin, "run_doctor") as doctor, \
                patch.object(signin, "emit") as emit:
            self.assertEqual(signin._run("doctor", "a"), 1)
        doctor.assert_not_called()
        self.assertEqual(emit.call_args.args[0]["reason"], "UID_MISMATCH")

    def test_snapshot_replaced_after_planning_is_still_rejected(self):
        contexts, warnings = signin.credential_plan()
        self.replace_a_with_b()
        seen = []

        def daily(headers, endpoint):
            seen.append(account_token(headers))
            return 0, {"result": "ALREADY", "report": "ok"}, True

        with patch.object(signin, "credential_plan", return_value=(contexts, warnings)), \
                patch.object(signin, "run_daily", side_effect=daily), \
                patch.object(signin, "emit"):
            self.assertEqual(signin._run("auto"), 1)
        self.assertEqual(seen, ["token-b"])


class RunRecordIdentityTests(AccountTestCase):
    def test_old_success_does_not_update_rebound_identity(self):
        self.bind("work", credential(uid=UID_A, token="token-a"))
        replacement = self.root / "replacement.info"
        replacement.write_text(json.dumps(credential(uid=UID_B, token="token-b")), encoding="utf-8")
        seen = []

        def daily(headers, endpoint):
            seen.append(account_token(headers))
            result = subprocess.run(
                [sys.executable, signin.__file__, "bind", "work", "--from", str(replacement), "--force"],
                capture_output=True, text=True, encoding="utf-8", timeout=10,
                env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            self.assertEqual(result.returncode, 0, result.stderr)
            return 0, {"result": "CLAIMED", "report": "A received 100",
                       "credits_gained": 100}, False

        with patch.object(signin, "run_daily", side_effect=daily), patch.object(signin, "emit"):
            self.assertEqual(signin._run("auto"), 0)
        entry = signin.find_account("work")
        self.assertEqual(seen, ["token-a"])
        self.assertEqual(entry["uid"], UID_B)
        self.assertEqual(entry["last_result"], "")
        self.assertEqual(entry["last_run_at"], "")

    def test_old_failure_does_not_update_same_uid_rebound_in_same_second(self):
        fixed = signin.datetime.fromisoformat("2026-11-13T12:00:00+08:00")

        def daily(headers, endpoint):
            self.bind("work", credential(uid=UID_A, token="token-fresh"), force=True)
            return 1, signin._auth_failure(401), False

        with patch.object(signin, "datetime", wraps=signin.datetime) as clock, \
                patch.object(signin, "run_daily", side_effect=daily), patch.object(signin, "emit"):
            clock.now.return_value = fixed
            self.bind("work", credential(uid=UID_A, token="token-old"))
            original = signin.find_account("work")
            self.assertEqual(signin._run("auto"), 1)
        current = signin.find_account("work")
        self.assertEqual(current["uid"], original["uid"])
        self.assertEqual(current["bound_at"], original["bound_at"])
        self.assertNotEqual(current["credential"], original["credential"])
        self.assertEqual(current["last_result"], "")
        self.assertEqual(current["last_run_at"], "")

    def test_old_run_does_not_update_unbound_then_recreated_name(self):
        self.bind("work", credential(uid=UID_A, token="token-a"))

        def daily(headers, endpoint):
            signin.unbind_account("work")
            self.bind("work", credential(uid=UID_B, token="token-b"))
            return 0, {"result": "ALREADY", "report": "old A"}, True

        with patch.object(signin, "run_daily", side_effect=daily), patch.object(signin, "emit"):
            self.assertEqual(signin._run("auto"), 0)
        current = signin.find_account("work")
        self.assertEqual(current["uid"], UID_B)
        self.assertEqual(current["last_result"], "")
        self.assertEqual(current["last_run_at"], "")


class FairnessTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.bind_ab()
        self.calls = []

    def fake_daily(self):
        def run(headers, endpoint):
            self.calls.append(account_token(headers))
            return 0, {"result": "CLAIMED", "report": "ok"}, True
        return run

    def test_deferred_account_runs_first_next_round(self):
        with patch.object(signin, "run_daily", side_effect=self.fake_daily()), \
                patch.object(signin, "emit"):
            # 第一轮：a 分到 30 秒跑完，剩 10 秒不够再开 b
            with patch.object(signin, "_budget_left", side_effect=[60.0, 10.0]):
                self.assertEqual(signin._run("auto"), 0)
            self.assertEqual(self.calls, ["token-a"])
            stamps = {a["name"]: a["last_run_at"]
                      for a in signin.load_registry()["accounts"]}
            self.assertTrue(stamps["a"])
            self.assertEqual(stamps["b"], "")

            # 第二轮：被推迟的 b（没跑过）应当排到前面
            self.calls.clear()
            with patch.object(signin, "_budget_left", side_effect=[60.0, 60.0]):
                self.assertEqual(signin._run("auto"), 0)
        self.assertEqual(self.calls, ["token-b", "token-a"])

    def test_tight_budget_alternates_instead_of_starving(self):
        with patch.object(signin, "run_daily", side_effect=self.fake_daily()), \
                patch.object(signin, "emit"):
            for expected in (["token-a"], ["token-b"], ["token-a"]):
                self.calls.clear()
                # 每轮都只够跑一个账号
                with patch.object(signin, "_budget_left", side_effect=[60.0, 10.0]):
                    self.assertEqual(signin._run("auto"), 0)
                self.assertEqual(self.calls, expected)


class IdentityFairnessTests(AccountTestCase):
    """同一 UID 的旧别名不能把整个身份永久钉在轮转第一位。"""

    def setUp(self):
        super().setUp()
        past = (time.time() - 3600) * 1000
        self.bind("old", credential(uid=UID_A, token="token-old", expires_at=past))
        self.bind("fresh", credential(uid=UID_A, token="token-fresh"), force=True)
        self.bind("b", credential(uid=UID_B, token="token-b"))
        signin.use_account("old")
        self.calls = []

    def fake_daily(self):
        def run(headers, endpoint):
            self.calls.append(account_token(headers))
            return 0, {"result": "CLAIMED", "report": "ok"}, True
        return run

    def test_expired_alias_cannot_pin_identity_to_first_place(self):
        with patch.object(signin, "run_daily", side_effect=self.fake_daily()), \
                patch.object(signin, "emit"):
            # 每轮都只够跑一个身份：有效快照跑过之后，独立账号 b 必须轮得到
            for expected in (["token-fresh"], ["token-b"], ["token-fresh"]):
                self.calls.clear()
                with patch.object(signin, "_budget_left", side_effect=[60.0, 10.0]):
                    self.assertEqual(signin._run("auto"), 0)
                self.assertEqual(self.calls, expected)

    def test_identity_order_uses_latest_alias_run_time(self):
        registry = signin.load_registry()
        by_name = {a["name"]: a for a in registry["accounts"]}
        # 旧清单状态：有效别名有运行时间，过期别名仍为空
        by_name["fresh"]["last_run_at"] = "2026-11-12T10:00:00+08:00"
        by_name["old"]["last_run_at"] = ""
        by_name["b"]["last_run_at"] = "2026-11-12T09:00:00+08:00"
        signin.save_registry(registry)

        contexts, _ = signin.credential_plan()
        self.assertEqual([c[0] for c in contexts], ["b", "fresh"])


class IdentityTieOrderTests(AccountTestCase):
    def test_equal_run_times_preserve_registry_order_instead_of_name_order(self):
        self.bind("z", credential(uid=UID_A, token="token-z"))
        self.bind("a", credential(uid=UID_B, token="token-a"))
        registry = signin.load_registry()
        for entry in registry["accounts"]:
            entry["last_run_at"] = "2026-11-12T10:00:00+08:00"
        signin.save_registry(registry)
        self.assertEqual([c[0] for c in signin.credential_plan()[0]], ["z", "a"])

    def test_equal_identity_times_ignore_unscheduled_alias_position(self):
        self.bind("z", credential(uid=UID_A, token="token-z"))
        self.bind("b", credential(uid=UID_B, token="token-b"))
        self.bind("a-alias", credential(uid=UID_B, token="token-alias"), force=True)
        registry = signin.load_registry()
        for entry in registry["accounts"][:2]:
            entry["last_run_at"] = "2026-11-12T10:00:00+08:00"
        signin.save_registry(registry)
        self.assertEqual([c[0] for c in signin.credential_plan()[0]], ["z", "a-alias"])


class CreditAggregationTests(AccountTestCase):
    def run_daily_with(self, auto, growth):
        with patch.object(signin, "run_auto", return_value=auto), \
                patch.object(signin, "run_growth", return_value=growth):
            return signin.run_daily({}, "https://example.com")

    def test_checkin_and_growth_credits_are_summed(self):
        code, out, _ = self.run_daily_with(
            (0, {"result": "CLAIMED", "credit": 100, "report": "领了"}),
            (0, {"result": "GROWTH", "report": "成长", "credits_gained": 50, "idle": False}))
        self.assertEqual(code, 0)
        self.assertEqual(out["credits_gained"], 150)
        self.assertEqual(out["growth_credits"], 50)

    def test_already_history_is_not_counted_as_new(self):
        code, out, _ = self.run_daily_with(
            (0, {"result": "ALREADY", "today_credit": 100, "total_credits": 700,
                 "report": "已签"}),
            (0, {"result": "GROWTH", "report": "成长", "credits_gained": 50, "idle": False}))
        self.assertEqual(code, 0)
        self.assertEqual(out["credits_gained"], 50)

    def test_multi_account_total_uses_real_run_daily_fields(self):
        self.bind_ab()

        def auto(headers, endpoint):
            if account_token(headers) == "token-a":
                return 0, {"result": "CLAIMED", "credit": 100, "report": "a 领"}
            return 0, {"result": "ALREADY", "today_credit": 100, "report": "b 已签"}

        with patch.object(signin, "run_auto", side_effect=auto), \
                patch.object(signin, "run_growth",
                             return_value=(0, {"result": "GROWTH", "report": "成长",
                                               "credits_gained": 50, "idle": False})), \
                patch.object(signin, "emit") as emit:
            self.assertEqual(signin._run("auto"), 0)
        self.assertEqual(emit.call_args.args[0]["credits_gained"], 200)


class ReservedNameTests(AccountTestCase):
    def test_reserved_names_cannot_be_bound(self):
        for name in ("current", "client", "CURRENT", "Client", "cLiEnT"):
            with self.subTest(name=name), self.assertRaises(signin.AccountsError):
                self.bind(name)
        self.assertFalse((self.accounts_root / "accounts.json").exists())

    def test_account_current_still_selects_the_client_login(self):
        self.bind("work")
        self.write_client(credential(uid=UID_LIVE, token="token-live"))
        seen = []

        def daily(headers, endpoint):
            seen.append(account_token(headers))
            return 0, {"result": "ALREADY", "report": "ok"}, True

        with patch.object(signin, "run_daily", side_effect=daily), patch.object(signin, "emit"):
            self.assertEqual(signin._run("auto", "current"), 0)
        self.assertEqual(seen, ["token-live"])


class ActiveFieldTests(AccountTestCase):
    def test_non_string_active_falls_back_to_client_login(self):
        self.accounts_root.mkdir(parents=True, exist_ok=True)
        for value in ([], {}, 0, True):
            with self.subTest(value=value):
                (self.accounts_root / "accounts.json").write_text(json.dumps(
                    {"version": 1, "active": value, "accounts": []}), encoding="utf-8")
                with self.assertRaises(signin.AccountsError):
                    signin.load_registry()

        with patch.object(signin, "run_daily",
                          return_value=(0, {"result": "ALREADY", "report": "今日已签过"}, True)), \
                patch.object(signin, "emit") as emit:
            self.assertEqual(signin._run("auto"), 0)
        self.assertIn("active", emit.call_args.args[0]["account_warnings"][0])


class CommandLineTests(AccountTestCase):
    def test_parse_argv_accepts_both_orders_and_flags(self):
        self.assertEqual(signin._parse_argv(["bind", "work", "--force"]),
                         ("bind", "work", None, True))
        self.assertEqual(signin._parse_argv(["bind", "work", "--from", "x.info"]),
                         ("bind", "work", "x.info", False))
        self.assertEqual(signin._parse_argv(["--account=work", "silent"]),
                         ("silent", "work", None, False))
        self.assertEqual(signin._parse_argv(["auto", "--account", "work"]),
                         ("auto", "work", None, False))
        self.assertEqual(signin._parse_argv([]), ("auto", None, None, False))
        self.assertEqual(signin._parse_argv(["--help"])[0], "help")

    def test_parse_argv_rejects_bad_flags(self):
        for argv in (["--account"], ["--from"], ["--unknown"], ["auto", "extra"],
                     ["auto", "--force"], ["accounts", "--from", "x.info"]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                signin._parse_argv(argv)

    def test_empty_explicit_selectors_report_error_without_running(self):
        self.bind_ab()
        for args in (["auto", "--account="], ["auto", "--account", ""],
                     ["auto", "--account", " "], ["--account=", "silent"]):
            with self.subTest(args=args), patch.object(signin, "run_daily") as daily, \
                    patch.object(signin, "emit") as emit:
                self.assertEqual(signin.main(args), 2)
                daily.assert_not_called()
                self.assertEqual(emit.call_args.args[0]["result"], "ERROR")

    def test_empty_explicit_sources_do_not_create_binding(self):
        for args in (["bind", "work", "--from="], ["bind", "work", "--from", ""],
                     ["bind", "work", "--from", " "]):
            with self.subTest(args=args), patch.object(signin, "find_auth_file") as find, \
                    patch.object(signin, "emit"):
                self.assertEqual(signin.main(args), 2)
                find.assert_not_called()
                self.assertEqual(signin.load_registry()["accounts"], [])

    def test_direct_empty_selector_is_also_rejected(self):
        self.bind_ab()
        with patch.object(signin, "run_daily") as daily, patch.object(signin, "emit"):
            self.assertEqual(signin._run("auto", ""), 1)
        daily.assert_not_called()

    def test_direct_empty_source_is_also_rejected(self):
        with self.assertRaises(signin.AccountsError):
            signin.bind_account("work", source="")
        self.assertEqual(signin.load_registry()["accounts"], [])

    def test_main_dispatches_account_commands(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            self.assertEqual(signin.main(["bind", "work"]), 0)
            self.assertEqual(signin.main(["accounts"]), 0)
            self.assertEqual(signin.main(["use", "work"]), 0)
            self.assertEqual(signin.main(["unbind", "work"]), 0)
            self.assertEqual(signin.main(["unbind", "work"]), 1)
        text = stdout.getvalue()
        for result in ("BOUND", "ACCOUNTS", "ACTIVE", "UNBOUND", "ERROR"):
            self.assertIn('"result": "%s"' % result, text)
        self.assertNotIn("token-a", text)

    def test_main_help_prints_usage(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            self.assertEqual(signin.main(["help"]), 0)
        self.assertIn("signin.py bind", stdout.getvalue())

    def test_bad_arguments_are_reported_not_raised(self):
        with patch.object(signin, "emit") as emit:
            self.assertEqual(signin.main(["bind"]), 1)
            self.assertEqual(emit.call_args.args[0]["result"], "ERROR")
            self.assertEqual(signin.main(["--nope"]), 2)
            self.assertEqual(emit.call_args.args[0]["result"], "ERROR")

    def test_missing_auth_override_keeps_the_actionable_diagnostic(self):
        self.client_file.unlink()
        emitted = []
        with patch.object(signin, "emit",
                          side_effect=lambda out, step: emitted.append(out)):
            self.assertEqual(signin._run("status"), 2)
        self.assertEqual(emitted[-1]["result"], "NO_AUTH")
        self.assertIn("WORKBUDDY_AUTH_FILE 指向的文件不存在", emitted[-1]["report"])


if __name__ == "__main__":
    unittest.main()

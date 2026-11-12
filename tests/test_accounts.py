"""多账号：账号清单、凭据快照、切换与轮转执行。

这些用例只操作临时目录里的假凭据，不联网、不碰真实客户端数据。
"""

import io
import json
import os
from pathlib import Path
import stat
import tempfile
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

    def test_order_puts_active_first_then_oldest(self):
        self.bind_ab()
        registry = signin.load_registry()
        registry["active"] = "b"
        registry["accounts"][0]["last_run_at"] = "2026-11-12T10:00:00+08:00"
        registry["accounts"][1]["last_run_at"] = "2026-11-12T09:00:00+08:00"
        signin.save_registry(registry)
        self.assertEqual([a["name"] for a in signin.order_accounts(signin.load_registry())],
                         ["b", "a"])


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

"""ServerChan daily reports: mocked HTTP only, no real credentials or pushes."""

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import signin


class Response:
    def __init__(self, body=b'{"code":0}', status=200):
        self.body = body
        self.status = status

    def read(self, limit):
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ServerChanTests(unittest.TestCase):
    KEY = "SCT123synthetic-secret"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "signin.log"
        environment = patch.dict(os.environ, {
            "WORKBUDDY_SERVERCHAN_SENDKEY": "",
            "WORKBUDDY_SIGNIN_LOG": str(self.log),
            "WORKBUDDY_BUDGET_SECONDS": "",
        })
        environment.start()
        self.addCleanup(environment.stop)
        sensitive = patch.object(signin, "_sensitive_values", set())
        sensitive.start()
        self.addCleanup(sensitive.stop)
        self.http = patch.object(signin.urllib.request, "urlopen", return_value=Response())
        self.urlopen = self.http.start()
        self.addCleanup(self.http.stop)

    def emit(self, out=None, action="silent"):
        out = out or {"result": "CLAIMED", "report": "成功领取 100 积分"}
        with patch("sys.stdout", io.StringIO()) as stdout:
            signin.emit(out, action)
        return stdout.getvalue()

    def last_log(self):
        line = self.log.read_text(encoding="utf-8").splitlines()[-1]
        return json.loads(line.split("] ", 1)[1])

    def test_disabled_by_default_keeps_original_output(self):
        self.emit()
        self.urlopen.assert_not_called()
        self.assertNotIn("notification_warning", self.last_log())

    def test_turbo_sends_only_redacted_report_without_auth_headers(self):
        os.environ["WORKBUDDY_SERVERCHAN_SENDKEY"] = self.KEY
        signin._sensitive_values.add("synthetic-access-token")
        self.emit({
            "result": "CLAIMED",
            "report": "已领取，synthetic-access-token，" + self.KEY,
            "claim_body": {"private": "must-not-be-forwarded"},
        })
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://sctapi.ftqq.com/" + self.KEY + ".send")
        self.assertEqual(self.urlopen.call_args.kwargs["timeout"], 5)
        payload = json.loads(request.data)
        self.assertEqual(set(payload), {"title", "desp"})
        self.assertIn("已领取", payload["desp"])
        self.assertEqual(payload["desp"].count("[REDACTED]"), 2)
        self.assertNotIn("must-not-be-forwarded", request.data.decode("utf-8"))
        self.assertNotIn("authorization", {key.lower() for key, _ in request.header_items()})
        self.assertNotIn(self.KEY, self.log.read_text(encoding="utf-8"))
        self.assertNotIn("synthetic-access-token", self.log.read_text(encoding="utf-8"))

    def test_longer_secret_is_not_partially_exposed_by_a_shorter_prefix(self):
        os.environ["WORKBUDDY_SERVERCHAN_SENDKEY"] = self.KEY
        signin._sensitive_values.add("SCT")
        self.emit({"result": "CLAIMED", "report": self.KEY})
        self.assertEqual(json.loads(self.urlopen.call_args.args[0].data)["desp"], "[REDACTED]")
        self.assertEqual(self.last_log()["report"], "[REDACTED]")

    def test_serverchan3_uses_official_user_id_route(self):
        os.environ["WORKBUDDY_SERVERCHAN_SENDKEY"] = "sctp12tsynthetic"
        self.emit()
        self.assertEqual(self.urlopen.call_args.args[0].full_url,
                         "https://12.push.ft07.com/send/sctp12tsynthetic.send")

    def test_key_is_encoded_as_a_path_value(self):
        os.environ["WORKBUDDY_SERVERCHAN_SENDKEY"] = "SCTsecret?channel=1"
        self.emit()
        self.assertEqual(self.urlopen.call_args.args[0].full_url,
                         "https://sctapi.ftqq.com/SCTsecret%3Fchannel%3D1.send")

    def test_invalid_key_warns_without_requesting_or_exposing_it(self):
        for key in ("https://evil.example/key", "sctpnot-a-user", "SCT", "SCTa\nb"):
            with self.subTest(key=key):
                os.environ["WORKBUDDY_SERVERCHAN_SENDKEY"] = key
                self.emit({"result": "CLAIMED", "report": "key=" + key})
                self.urlopen.assert_not_called()
                self.assertIn("notification_warning", self.last_log())
                self.assertNotIn(key, self.last_log()["report"])

    def test_missing_or_non_string_report_does_not_send(self):
        os.environ["WORKBUDDY_SERVERCHAN_SENDKEY"] = self.KEY
        for value in (None, "", "  ", {"raw": "data"}):
            with self.subTest(value=value):
                self.emit({"result": "CLAIMED", "report": value})
        self.urlopen.assert_not_called()

    def test_only_daily_commands_push(self):
        os.environ["WORKBUDDY_SERVERCHAN_SENDKEY"] = self.KEY
        for action in ("status", "claim", "all", "doctor", "accounts", "bind", "use",
                       "unbind", "help", "growth", "silent-poll", "silent-growth"):
            with self.subTest(action=action):
                self.emit(action=action)
        self.urlopen.assert_not_called()
        self.emit(action="auto")
        self.emit(action="silent")
        self.assertEqual(self.urlopen.call_count, 2)

    def test_bad_http_or_business_response_is_not_reported_as_success(self):
        os.environ["WORKBUDDY_SERVERCHAN_SENDKEY"] = self.KEY
        for body, status in ((b'{"code":0}', 503), (b'{"code":1}', 200),
                             (b'{"code":false}', 200), (b'{"code":"0"}', 200),
                             (b'{}', 200), (b'[]', 200), (b'not json', 200)):
            with self.subTest(body=body, status=status):
                self.urlopen.return_value = Response(body, status)
                self.emit()
                self.assertIn("notification_warning", self.last_log())
                self.assertEqual(self.last_log()["result"], "CLAIMED")
                self.assertFalse(self.last_log()["needs_attention"])

    def test_push_failure_is_safe_and_not_retried(self):
        os.environ["WORKBUDDY_SERVERCHAN_SENDKEY"] = self.KEY
        self.urlopen.side_effect = OSError("private URL includes " + self.KEY)
        out = {"result": "CLAIMED", "report": "claimed"}
        self.emit(out)
        self.assertEqual(self.urlopen.call_count, 1)
        self.assertNotIn("notification_warning", out)
        logged = self.last_log()
        self.assertIn("notification_warning", logged)
        self.assertNotIn(self.KEY, json.dumps(logged))
        self.assertFalse(logged["needs_attention"])

    def test_main_preserves_business_exit_code_when_push_fails(self):
        os.environ["WORKBUDDY_SERVERCHAN_SENDKEY"] = self.KEY
        self.urlopen.side_effect = OSError("offline")
        with patch.object(signin, "credential_plan",
                          return_value=([(None, "synthetic.info", None)], [])), \
                patch.object(signin, "_record_account_run"), \
                patch.object(signin, "_run_one_account") as run:
            for code, result in ((0, "CLAIMED"), (1, "NETWORK")):
                with self.subTest(code=code):
                    run.return_value = (code, {"result": result, "report": "primary result",
                                              "needs_attention": code != 0}, False, [])
                    self.assertEqual(signin.main(["silent"]), code)
                    logged = self.last_log()
                    self.assertEqual(logged["result"], result)
                    self.assertEqual(logged["needs_attention"], code != 0)
                    self.assertIn("notification_warning", logged)

    def test_multi_account_daily_run_sends_one_final_summary(self):
        os.environ["WORKBUDDY_SERVERCHAN_SENDKEY"] = self.KEY
        contexts = [("a", "a.info", None), ("b", "b.info", None)]
        with patch.object(signin, "credential_plan", return_value=(contexts, [])), \
                patch.object(signin, "_record_account_run"), \
                patch.object(signin, "_run_one_account", side_effect=[
                    (0, {"result": "CLAIMED", "report": "a claimed", "credits_gained": 1}, False, []),
                    (0, {"result": "CLAIMED", "report": "b claimed", "credits_gained": 2}, False, []),
                ]):
            self.assertEqual(signin.main(["silent"]), 0)
        self.assertEqual(self.urlopen.call_count, 1)
        report = json.loads(self.urlopen.call_args.args[0].data)["desp"]
        self.assertIn("a claimed", report)
        self.assertIn("b claimed", report)
        self.assertEqual(self.last_log()["credits_gained"], 3)


if __name__ == "__main__":
    unittest.main()

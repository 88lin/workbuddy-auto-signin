"""WorkBuddy AI 版客户端：凭据路径、默认服务地址与运行时配对。

只用临时目录里的假凭据与模拟请求，不联网、不执行任何客户端。
"""

import base64
import json
import ntpath
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import signin
from test_runtime_discovery import FakeRegistry, entry


def ai_session(token="synthetic-token", **auth):
    """AI 版 5.9.4 实测形态：没有 auth.endpoint，只有 auth.domain。"""
    return {"auth": dict({"accessToken": token, "domain": "www.workbuddy.ai"}, **auth),
            "account": {"uid": "synthetic-user", "enterpriseId": ""}}


def cn_session(token="synthetic-token", **auth):
    return {"auth": dict({"accessToken": token, "domain": "www.workbuddy.cn"}, **auth),
            "account": {"uid": "synthetic-user", "enterpriseId": ""}}


ENVELOPE = {"$wbEncrypted": 1, "envelope": base64.b64encode(json.dumps({
    "suite": 1, "keyId": "0123456789abcdef",
    "nonce": base64.b64encode(b"\x00" * 12).decode(),
    "authTag": base64.b64encode(b"\x01" * 16).decode(),
    "ciphertext": base64.b64encode(b"payload").decode()}).encode()).decode()}


class EndpointTests(unittest.TestCase):
    def test_ai_domain_without_endpoint_uses_ai_backend(self):
        self.assertEqual(signin.session_endpoint(ai_session()), "https://www.workbuddy.ai")
        self.assertEqual(signin.session_endpoint(ai_session(domain="WWW.WorkBuddy.AI")),
                         "https://www.workbuddy.ai")

    def test_domestic_default_is_unchanged(self):
        self.assertEqual(signin.session_endpoint(cn_session()), signin.DEFAULT_ENDPOINT)
        no_domain = cn_session()
        del no_domain["auth"]["domain"]
        self.assertEqual(signin.session_endpoint(no_domain), signin.DEFAULT_ENDPOINT)

    def test_explicit_endpoint_always_wins(self):
        self.assertEqual(signin.session_endpoint(ai_session(endpoint="https://example.com/")),
                         "https://example.com")
        self.assertEqual(signin.session_endpoint(cn_session(endpoint="https://www.workbuddy.ai")),
                         "https://www.workbuddy.ai")

    def test_edition_detection(self):
        self.assertEqual(signin._session_edition(ai_session()), signin.EDITION_AI)
        self.assertEqual(signin._session_edition(cn_session(endpoint="https://www.workbuddy.ai/")),
                         signin.EDITION_AI)
        for value in (cn_session(), cn_session(endpoint="https://copilot.tencent.com"),
                      cn_session(domain="workbuddy.ai.example.com"), {}, [], {"auth": []}):
            with self.subTest(value=value):
                self.assertEqual(signin._session_edition(value), signin.EDITION_CN)

    def test_ai_session_validates_and_keeps_domain_header(self):
        resolved = signin.resolve_session(ai_session())
        self.assertEqual(signin.build_headers(resolved)["X-Domain"], "www.workbuddy.ai")


class RunTests(unittest.TestCase):
    def setUp(self):
        signin._start_budget()
        signin._sensitive_values.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.auth_file = root / "workbuddy-desktop-ai.info"
        self.environment = patch.dict(os.environ, {
            "WORKBUDDY_AUTH_FILE": str(self.auth_file),
            "WORKBUDDY_SIGNIN_LOG": str(root / "signin.log"),
            "WORKBUDDY_ACCOUNTS_DIR": str(root / "accounts"),
            "WORKBUDDY_BUDGET_SECONDS": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_status_requests_go_to_ai_backend(self):
        self.auth_file.write_text(json.dumps(ai_session()), encoding="utf-8")
        with patch.object(signin, "post", return_value=(200, {"todayCheckedIn": True})) as post, \
                patch.object(signin, "emit"):
            signin._run("status")
        self.assertTrue(post.call_args_list)
        for call in post.call_args_list:
            self.assertTrue(call.args[0].startswith("https://www.workbuddy.ai/v2/"), call.args[0])

    def test_bind_records_ai_endpoint(self):
        self.auth_file.write_text(json.dumps(ai_session()), encoding="utf-8")
        signin.bind_account("intl")
        entry = signin.load_registry()["accounts"][0]
        self.assertEqual(entry["endpoint"], "https://www.workbuddy.ai")

    def test_encrypted_ai_session_asks_for_ai_runtime(self):
        for value, edition in ((ai_session(ENVELOPE), signin.EDITION_AI),
                               (cn_session(ENVELOPE), signin.EDITION_CN)):
            with self.subTest(edition=edition), \
                    patch.object(signin, "find_workbuddy_runtime", return_value="runtime") as runtime, \
                    patch.object(signin, "_run_auth_helper", return_value={"accessToken": "plain"}):
                signin.resolve_session(value)
            runtime.assert_called_once_with(edition)

    def test_doctor_probes_matching_runtime(self):
        with patch.object(signin, "find_workbuddy_runtime", return_value="runtime") as runtime, \
                patch.object(signin, "_run_auth_helper", return_value={"electron": "37.10.3"}):
            signin.run_doctor(ai_session(ENVELOPE))
        runtime.assert_called_once_with(signin.EDITION_AI)


class AuthFileDiscoveryTests(unittest.TestCase):
    def find(self, existing):
        local = r"C:\Users\someone\AppData\Local"
        paths = {os.path.join(local, name) for name in existing}
        with patch.dict(os.environ, {"WORKBUDDY_AUTH_FILE": "", "LOCALAPPDATA": local}), \
                patch("os.path.exists", side_effect=lambda path: path in paths):
            return signin.find_auth_file()

    def test_ai_credential_is_found_when_domestic_is_absent(self):
        path, looked_in = self.find([signin.AI_AUTH_BASENAME])
        self.assertTrue(path.endswith("workbuddy-desktop-ai.info"))
        self.assertIn(path, looked_in)

    def test_domestic_credential_keeps_priority(self):
        path, _ = self.find([signin.AUTH_BASENAME, signin.AI_AUTH_BASENAME])
        self.assertTrue(path.endswith("workbuddy-desktop.info"))


class RuntimeDiscoveryTests(unittest.TestCase):
    LOCAL = r"C:\Users\someone\AppData\Local"
    AI_EXE = r"C:\Program Files\WorkBuddyAI\WorkBuddyAI.exe"
    CN_EXE = r"C:\Program Files\WorkBuddy\WorkBuddy.exe"

    def discover(self, files, edition, registry=None, full=True):
        existing = {ntpath.normcase(path) for path in files}
        environ = {"WORKBUDDY_EXE": "", "LOCALAPPDATA": self.LOCAL,
                   "ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": ""}
        with patch.object(sys, "platform", "win32"), \
                patch.dict(sys.modules, {"winreg": registry or FakeRegistry()}), \
                patch.dict(os.environ, environ), \
                patch("os.path.join", ntpath.join), patch("os.path.abspath", lambda p: p), \
                patch("os.path.isfile", side_effect=lambda path: ntpath.normcase(path) in existing), \
                patch.object(signin.subprocess, "Popen", side_effect=AssertionError("must not execute")):
            if full:
                return signin.find_workbuddy_runtime(edition)
            return list(signin._windows_registry_runtimes(edition))

    def test_ai_runtime_in_program_files_and_local_programs(self):
        self.assertEqual(self.discover([self.AI_EXE], signin.EDITION_AI), self.AI_EXE)
        local_exe = ntpath.join(self.LOCAL, "Programs", "WorkBuddyAI", "WorkBuddyAI.exe")
        self.assertEqual(self.discover([local_exe], signin.EDITION_AI), local_exe)

    def test_editions_never_borrow_each_others_runtime(self):
        for files, edition in (([self.AI_EXE], signin.EDITION_CN), ([self.CN_EXE], signin.EDITION_AI)):
            with self.subTest(edition=edition), self.assertRaises(signin.AuthError) as caught:
                self.discover(files, edition)
            self.assertEqual(caught.exception.reason, "RUNTIME_NOT_FOUND")
        both = [self.AI_EXE, self.CN_EXE]
        self.assertEqual(self.discover(both, signin.EDITION_AI), self.AI_EXE)
        self.assertEqual(self.discover(both, signin.EDITION_CN), self.CN_EXE)

    def test_default_edition_is_domestic(self):
        with patch.object(sys, "platform", "win32"), \
                patch.dict(os.environ, {"WORKBUDDY_EXE": "", "LOCALAPPDATA": self.LOCAL,
                                        "ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": ""}), \
                patch("os.path.join", ntpath.join), patch("os.path.abspath", lambda p: p), \
                patch("os.path.isfile", side_effect=lambda p: ntpath.normcase(p) == ntpath.normcase(self.CN_EXE)):
            self.assertEqual(signin.find_workbuddy_runtime(), self.CN_EXE)

    def test_registry_entry_of_ai_client(self):
        path = r"D:\Apps\WorkBuddyAI\WorkBuddyAI.exe"
        for record in (entry("WorkBuddy AI 5.9.4", icon=path + ",0"),
                       entry("WorkBuddyAI", location=r"D:\Apps\WorkBuddyAI"),
                       entry("workbuddy ai 6.0.0-beta.1", icon='"' + path + '"')):
            with self.subTest(record=record):
                registry = FakeRegistry({("machine", 256): [record]})
                self.assertEqual(self.discover([path], signin.EDITION_AI, registry, full=False), [path])
                registry = FakeRegistry({("machine", 256): [record]})
                self.assertEqual(self.discover([path], signin.EDITION_CN, registry, full=False), [])

    def test_ai_registry_rejects_domestic_and_unrelated_entries(self):
        cn = r"D:\Apps\WorkBuddy\WorkBuddy.exe"
        ai = r"D:\Apps\WorkBuddyAI\WorkBuddyAI.exe"
        for record in (entry("WorkBuddy 5.6.2", icon=cn), entry("WorkBuddy AI 5.9.4", icon=cn),
                       entry("WorkBuddy AI Helper", icon=ai), entry("WorkBuddy 5.6.2", icon=ai)):
            with self.subTest(record=record):
                registry = FakeRegistry({("user", 256): [record]})
                self.assertEqual(self.discover([cn, ai], signin.EDITION_AI, registry, full=False), [])

    def test_override_still_wins_for_ai(self):
        with tempfile.TemporaryDirectory() as temp:
            exe = os.path.join(temp, "custom.exe")
            Path(exe).write_bytes(b"")
            with patch.dict(os.environ, {"WORKBUDDY_EXE": exe}):
                self.assertEqual(signin.find_workbuddy_runtime(signin.EDITION_AI), os.path.abspath(exe))

    def test_macos_ai_has_no_implicit_runtime(self):
        with patch.object(sys, "platform", "darwin"), patch.dict(os.environ, {"WORKBUDDY_EXE": ""}), \
                patch.object(signin, "_mac_runtime", return_value="/Applications/WorkBuddy.app/Contents/MacOS/WorkBuddy"), \
                patch("os.path.isfile", return_value=True), patch("os.access", return_value=True):
            with self.assertRaises(signin.AuthError):
                signin.find_workbuddy_runtime(signin.EDITION_AI)
            self.assertIn("WorkBuddy", signin.find_workbuddy_runtime(signin.EDITION_CN))


if __name__ == "__main__":
    unittest.main()

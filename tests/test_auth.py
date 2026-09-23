import base64
import copy
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import signin


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sym-v1.json").read_text(encoding="utf-8"))
NODE = shutil.which("node")


def session(token="synthetic-token"):
    return {"auth": {"accessToken": token, "refreshToken": {"broken": True},
                     "endpoint": "https://copilot.tencent.com", "domain": "www.workbuddy.cn"},
            "account": {"uid": "synthetic-user", "enterpriseId": "synthetic-enterprise"}}


def wrapper(**changes):
    inner = dict(FIXTURE["envelope"], **changes)
    return {"$wbEncrypted": 1, "envelope": base64.b64encode(json.dumps(inner).encode()).decode()}


class AuthTests(unittest.TestCase):
    def setUp(self):
        signin._start_budget()
        signin._sensitive_values.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.auth_file = Path(self.temp.name) / "session.json"
        self.auth_file.write_text(json.dumps(session()), encoding="utf-8")
        self.environment = patch.dict(os.environ, {
            "WORKBUDDY_AUTH_FILE": str(self.auth_file),
            "WORKBUDDY_SIGNIN_LOG": str(Path(self.temp.name) / "signin.log"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_plaintext_preserves_metadata_without_runtime_or_mutation(self):
        value = session()
        before = copy.deepcopy(value)
        with patch.object(signin, "find_workbuddy_runtime") as runtime:
            resolved = signin.resolve_session(value)
        runtime.assert_not_called()
        self.assertEqual(value, before)
        self.assertIsNot(resolved, value)
        headers = signin.build_headers(resolved)
        self.assertEqual(headers["Authorization"], "Bearer synthetic-token")
        self.assertEqual(headers["X-User-Id"], "synthetic-user")
        self.assertEqual(headers["X-Enterprise-Id"], "synthetic-enterprise")
        self.assertEqual(headers["X-Domain"], "www.workbuddy.cn")

    def test_invalid_tokens_never_reach_network(self):
        values = [[], 42, True, {}, {"$wbEncrypted": 2, "envelope": "AA=="},
                  {"$wbEncrypted": True, "envelope": "AA=="}, "a\nb", " a", "{'token': 1}"]
        for value in values:
            with self.subTest(value=value):
                self.auth_file.write_text(json.dumps(session(value)), encoding="utf-8")
                with patch.object(signin, "post") as post, patch.object(signin, "emit") as emit:
                    self.assertNotEqual(signin._run("status"), 0)
                post.assert_not_called()
                self.assertEqual(emit.call_args.args[0]["result"], "AUTH_ERROR")

    def test_headers_reject_unresolved_envelope(self):
        with self.assertRaises(signin.AuthError):
            signin.build_headers(session(FIXTURE["wrapper"]))

    def test_malformed_sessions_and_header_injection_are_rejected(self):
        values = [[], 1, {"auth": [], "account": {}}, {"auth": {}, "account": []}]
        for field in ("uid", "enterpriseId"):
            value = session()
            value["account"][field] = "x\r\nInjected: yes"
            values.append(value)
        for endpoint in ([], "http://host", "https://user:pass@host", "https://host:bad", "https://host\n"):
            value = session()
            value["auth"]["endpoint"] = endpoint
            values.append(value)
        for value in values:
            with self.subTest(value=value), self.assertRaises(signin.AuthError):
                signin.resolve_session(value)

    def test_optional_empty_headers_remain_compatible(self):
        value = session()
        value["account"]["enterpriseId"] = ""
        value["auth"]["domain"] = ""
        self.assertNotIn("X-Domain", signin.build_headers(signin.resolve_session(value)))

    def test_envelope_validation_before_runtime(self):
        invalid = [wrapper(suite=2), wrapper(suite=True), wrapper(keyId="Z" * 16),
                   wrapper(nonce="AA=="), wrapper(authTag="AA=="), wrapper(ciphertext="???"),
                   {"$wbEncrypted": 1, "envelope": base64.b64encode(b"[]").decode()},
                   {"$wbEncrypted": 1, "envelope": "A" * signin.AUTH_INPUT_LIMIT}]
        for value in invalid:
            with self.subTest(value=value), patch.object(signin, "find_workbuddy_runtime") as runtime:
                with self.assertRaises(signin.AuthError):
                    signin.resolve_session(session(value))
                runtime.assert_not_called()

    def test_encrypted_access_ignores_broken_refresh(self):
        original = session(FIXTURE["wrapper"])
        before = copy.deepcopy(original)
        with patch.object(signin, "find_workbuddy_runtime", return_value="runtime"), \
                patch.object(signin, "_run_auth_helper", return_value={"accessToken": FIXTURE["token"]}) as helper:
            resolved = signin.resolve_session(original)
        self.assertEqual(resolved["auth"]["accessToken"], FIXTURE["token"])
        self.assertEqual(original, before)
        self.assertEqual(set(helper.call_args.args[1]), {"operation", "value"})

    def test_doctor_only_probes_and_does_not_request_network(self):
        with patch.object(signin, "find_workbuddy_runtime", return_value="runtime"), \
                patch.object(signin, "_run_auth_helper", return_value={"electron": "37.10.3"}) as helper, \
                patch.object(signin, "post") as post:
            out = signin.run_doctor(session(FIXTURE["wrapper"]))
        self.assertFalse(out["online_checked"])
        self.assertEqual(helper.call_args.args[1], {"operation": "probe"})
        self.assertNotIn("accessToken", json.dumps(out))
        post.assert_not_called()

    def test_all_silent_modes_log_auth_errors(self):
        self.auth_file.write_text(json.dumps(session(wrapper(suite=99))), encoding="utf-8")
        before = self.auth_file.read_bytes()
        for action in ("silent", "silent-poll", "silent-growth"):
            with patch.object(signin, "post") as post:
                self.assertEqual(signin._run(action), 1)
            post.assert_not_called()
        log = Path(os.environ["WORKBUDDY_SIGNIN_LOG"]).read_text(encoding="utf-8")
        self.assertEqual(log.count('"result": "AUTH_ERROR"'), 3)
        self.assertNotIn("envelope", log)
        self.assertEqual(self.auth_file.read_bytes(), before)

    def test_output_redacts_token_even_in_nested_errors(self):
        signin.resolve_session(session())
        out = io.StringIO()
        with patch("sys.stdout", out):
            signin.emit({"body": {"error": "echo synthetic-token"}}, "status")
        self.assertNotIn("synthetic-token", out.getvalue())
        self.assertIn("[REDACTED]", out.getvalue())

    def test_auth_rejection_stops_daily_work(self):
        for status, result in ((401, "AUTH_REJECTED"), (403, "FORBIDDEN")):
            with self.subTest(status=status), patch.object(signin, "post", return_value=(status, {})) as post, \
                    patch.object(signin, "run_growth") as growth:
                code, out, quiet = signin.run_daily({}, "https://example.com")
            self.assertEqual((code, out["result"], quiet), (1, result, False))
            self.assertEqual(post.call_count, 1)
            growth.assert_not_called()

    def test_successful_signin_is_preserved_if_growth_auth_fails(self):
        with patch.object(signin, "run_auto", return_value=(0, {"result": "CLAIMED", "report": "claimed"})), \
                patch.object(signin, "run_growth", return_value=(1, signin._auth_failure(401))):
            code, out, quiet = signin.run_daily({}, "https://example.com")
        self.assertEqual(code, 1)
        self.assertEqual(out["result"], "CLAIMED")
        self.assertEqual(out["growth_result"], "AUTH_REJECTED")
        self.assertTrue(out["needs_attention"])
        self.assertFalse(quiet)

    @unittest.skipUnless(os.name == "nt", "Windows pythonw integration")
    def test_pythonw_silent_poll_logs_without_console(self):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.is_file():
            self.skipTest("pythonw is not installed")
        self.auth_file.write_text(json.dumps(session(wrapper(suite=99))), encoding="utf-8")
        result = subprocess.run([str(pythonw), str(Path(signin.__file__).resolve()), "silent-poll"],
                                timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 1)
        log = Path(os.environ["WORKBUDDY_SIGNIN_LOG"]).read_text(encoding="utf-8")
        self.assertIn('"reason": "UNSUPPORTED_ENVELOPE"', log)
        self.assertNotIn(FIXTURE["token"], log)

    def test_growth_classifies_auth_and_permission_failures(self):
        for status, result in ((401, "AUTH_REJECTED"), (403, "FORBIDDEN")):
            with self.subTest(status=status), patch.object(signin, "get", return_value=(status, {})) as get, \
                    patch.object(signin, "post") as post:
                code, out = signin.run_growth({}, "https://example.com")
            self.assertEqual((code, out["result"]), (1, result))
            self.assertEqual(get.call_count, 1)
            post.assert_not_called()

    def test_business_forbidden_stays_locked(self):
        self.assertTrue(signin._is_tier_locked(403, {"msg": "连续登录天数不足"}))
        self.assertFalse(signin._is_tier_locked(403, {"msg": "permission denied"}))

    def test_debug_failures_are_nonzero_and_all_does_not_claim(self):
        for status in (401, 403, 500, signin.CODE_NO_NETWORK):
            with self.subTest(status=status), patch.object(signin, "post", return_value=(status, {})) as post, \
                    patch.object(signin, "emit"):
                self.assertEqual(signin._run("all"), 1)
                self.assertEqual(post.call_count, 1)

    def test_non_idempotent_post_is_not_retried(self):
        with patch.object(signin, "_request", return_value=(signin.CODE_NO_NETWORK, {})) as request:
            signin.post("https://example.com/claim", {}, {"id": 1})
        self.assertEqual(request.call_count, 1)

    def test_override_never_silently_falls_back(self):
        with patch.dict(os.environ, {"WORKBUDDY_EXE": str(Path(self.temp.name) / "missing")}):
            with self.assertRaises(signin.AuthError) as caught:
                signin.find_workbuddy_runtime()
        self.assertEqual(caught.exception.reason, "INVALID_RUNTIME_PATH")

    def test_macos_uses_bundle_executable(self):
        bundle = Path(self.temp.name) / "WorkBuddy.app"
        (bundle / "Contents").mkdir(parents=True)
        with (bundle / "Contents" / "Info.plist").open("wb") as f:
            plistlib.dump({"CFBundleExecutable": "WorkBuddy Custom"}, f)
        self.assertEqual(signin._mac_runtime(str(bundle)), str(bundle / "Contents" / "MacOS" / "WorkBuddy Custom"))

    def test_platform_runtime_discovery(self):
        for platform in ("win32", "darwin", "linux"):
            with self.subTest(platform=platform), patch.dict(os.environ, {"WORKBUDDY_EXE": ""}), \
                    patch.object(sys, "platform", platform), patch("os.path.isfile", return_value=True), \
                    patch("os.access", return_value=True), patch.object(signin, "_mac_runtime", return_value="/Applications/WorkBuddy.app/Contents/MacOS/WorkBuddy"):
                if platform == "linux":
                    with self.assertRaises(signin.AuthError):
                        signin.find_workbuddy_runtime()
                else:
                    self.assertIn("WorkBuddy", signin.find_workbuddy_runtime())

    def test_linux_cli_auth_path(self):
        with patch.dict(os.environ, {"WORKBUDDY_AUTH_FILE": "", "XDG_DATA_HOME": self.temp.name}):
            expected = os.path.join(self.temp.name, signin.CLI_AUTH_BASENAME)
            with patch("os.path.exists", side_effect=lambda path: path == expected):
                self.assertEqual(signin.find_auth_file()[0], expected)


@unittest.skipUnless(NODE, "Node is needed only to test the embedded helper")
class HelperTests(unittest.TestCase):
    def setUp(self):
        signin._start_budget()
        self.stub = "process._linkedBinding = () => ({loggerGet: () => " + json.dumps(json.dumps(FIXTURE["build_payload"])) + "});\n"

    def run_helper(self, value=None, code=None, operation="decrypt"):
        code = self.stub + signin.AUTH_HELPER_JS if code is None else code
        request = {"operation": operation}
        if operation == "decrypt":
            request["value"] = value or FIXTURE["wrapper"]
        with patch.object(signin, "AUTH_HELPER_JS", code):
            return signin._run_auth_helper(NODE, request)

    def assert_failure(self, reason, **kwargs):
        with self.assertRaises(signin.AuthError) as caught:
            self.run_helper(**kwargs)
        self.assertEqual(caught.exception.reason, reason)
        self.assertNotIn(FIXTURE["token"], str(caught.exception))
        self.assertNotIn(FIXTURE["build_payload"]["atRestSecretKey"], str(caught.exception))

    def test_independent_aesgcm_fixture(self):
        self.assertEqual(self.run_helper()["accessToken"], FIXTURE["token"])

    def test_ciphertext_tampering_fails_authentication(self):
        ciphertext = bytearray(base64.b64decode(FIXTURE["envelope"]["ciphertext"]))
        ciphertext[0] ^= 1
        self.assert_failure("DECRYPT_FAILED", value=wrapper(ciphertext=base64.b64encode(ciphertext).decode()))

    def test_key_mismatch_and_unsupported_suite(self):
        self.assert_failure("KEY_MISMATCH", value=wrapper(keyId="0" * 16))
        self.assert_failure("UNSUPPORTED_ENVELOPE", value=wrapper(suite=2))

    def test_invalid_envelopes_are_rejected_in_child(self):
        for value in (wrapper(nonce="AA=="), wrapper(authTag="AA=="), wrapper(ciphertext="?"),
                      {"$wbEncrypted": 1, "envelope": "W10="}):
            with self.subTest(value=value):
                self.assert_failure("INVALID_FORMAT", value=value)

    def test_missing_native_binding_has_fixed_error(self):
        self.assert_failure("RUNTIME_UNAVAILABLE", code=signin.AUTH_HELPER_JS)

    def test_probe_never_reads_key_material(self):
        code = "process._linkedBinding=()=>({loggerGet:()=>{throw Error('must not run')}});\n" + signin.AUTH_HELPER_JS
        self.assertTrue(self.run_helper(code=code, operation="probe")["ok"])

    def test_protocol_failures_do_not_expose_captured_output(self):
        cases = ["process.stdout.write('sensitive-noise');", "process.exit(0);",
                 "process.stdout.write(JSON.stringify({version:2,ok:true,accessToken:'synthetic-token'}));",
                 "process.stdout.write(JSON.stringify({version:1,ok:true,accessToken:{bad:1}}));",
                 "process.stdout.write(JSON.stringify({version:1,ok:false,reason:'sensitive-noise'}));process.exitCode=1;"]
        for code in cases:
            with self.subTest(code=code):
                self.assert_failure("HELPER_PROTOCOL", code=code)

    def test_output_limits(self):
        for stream, limit in (("stdout", signin.AUTH_OUTPUT_LIMIT), ("stderr", 8192)):
            with self.subTest(stream=stream):
                self.assert_failure("HELPER_PROTOCOL", code="process.%s.write('x'.repeat(%d));setInterval(()=>{},1000);" % (stream, limit + 1))

    def test_timeout_kills_and_reaps_process(self):
        processes = []
        original = subprocess.Popen
        def spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        before = time.monotonic()
        with patch.object(signin, "AUTH_HELPER_TIMEOUT", 0.2), patch.object(signin.subprocess, "Popen", side_effect=spawn):
            self.assert_failure("HELPER_TIMEOUT", code="setInterval(()=>{},1000);")
        self.assertLess(time.monotonic() - before, 3)
        self.assertIsNotNone(processes[0].poll())

    def test_total_budget_prevents_start(self):
        with patch.object(signin, "_budget_left", return_value=0), patch.object(signin.subprocess, "Popen") as popen:
            self.assert_failure("HELPER_TIMEOUT")
        popen.assert_not_called()

    def test_runtime_environment_and_argv_contain_no_credentials(self):
        calls = []
        original = subprocess.Popen
        def spawn(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)
        with patch.dict(os.environ, {"NODE_OPTIONS": "--invalid", "WORKBUDDY_SECRET": "sensitive-env"}), \
                patch.object(signin.subprocess, "Popen", side_effect=spawn):
            self.run_helper()
        args, kwargs = calls[0]
        self.assertNotIn("NODE_OPTIONS", kwargs["env"])
        self.assertNotIn("WORKBUDDY_SECRET", kwargs["env"])
        self.assertNotIn(FIXTURE["wrapper"]["envelope"], str(args))
        self.assertFalse(kwargs["shell"])
        if os.name == "nt":
            self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()

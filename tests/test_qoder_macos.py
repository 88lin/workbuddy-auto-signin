"""macOS 登录态读取的单元测试。

解密本身在客户端自带的 Electron 运行时里执行；测试时用普通 node 跑同一段
JS（CI 已装 node，见 .github/workflows/tests.yml），因此这里既能验证真实
解密结果，也能在 Linux/Windows 的 CI 上跑通。
"""

import base64
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import qoder_signin as qoder


NODE = shutil.which("node")
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "qoder-mac-auth.json").read_text(encoding="utf-8"))


def run_helper(auth_file, password, node=None):
    """直接以 node 运行内嵌 helper，返回原始进程结果。"""
    request = json.dumps({"version": 1, "password": password, "authFile": str(auth_file)})
    return subprocess.run([node or NODE, "-e", qoder.MAC_AUTH_HELPER_JS],
                          input=request.encode("utf-8"),
                          capture_output=True, timeout=30)


class MacHelperTests(unittest.TestCase):
    """内嵌 JS 的真实解密行为（生产环境由客户端 Electron 运行时执行）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def blob_file(self, case, name="auth.v1.dat"):
        path = Path(self.temp.name) / name
        path.write_bytes(base64.b64decode(FIXTURE["cases"][case]["blob"]))
        return path

    @unittest.skipUnless(NODE, "需要 node 作为测试夹具（生产用客户端自带运行时）")
    def test_decrypts_valid_session(self):
        process = run_helper(self.blob_file("valid"), FIXTURE["password"])
        reply = json.loads(process.stdout.decode("utf-8"))
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["session"]["token"],
                         FIXTURE["cases"]["valid"]["expect_token"])
        self.assertEqual(reply["session"]["expiresAt"], "2099-01-01T00:00:19Z")
        self.assertEqual(set(reply["session"]), {"token", "expiresAt"})
        self.assertEqual(process.returncode, 0)

    @unittest.skipUnless(NODE, "需要 node 作为测试夹具（生产用客户端自带运行时）")
    def test_wrong_password_reports_decrypt_failure(self):
        process = run_helper(self.blob_file("valid"), "not-the-keychain-password")
        reply = json.loads(process.stdout.decode("utf-8"))
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["reason"], "DECRYPT_FAILED")
        self.assertEqual(process.returncode, 1)

    @unittest.skipUnless(NODE, "需要 node 作为测试夹具（生产用客户端自带运行时）")
    def test_missing_auth_file(self):
        process = run_helper(Path(self.temp.name) / "absent", FIXTURE["password"])
        self.assertEqual(json.loads(process.stdout.decode("utf-8"))["reason"], "AUTH_MISSING")

    @unittest.skipUnless(NODE, "需要 node 作为测试夹具（生产用客户端自带运行时）")
    def test_unsupported_prefix_and_truncated_blob(self):
        for case, reason in (("wrong_prefix", "UNSUPPORTED_FORMAT"),
                             ("truncated", "DECRYPT_FAILED")):
            process = run_helper(self.blob_file(case), FIXTURE["password"])
            reply = json.loads(process.stdout.decode("utf-8"))
            self.assertFalse(reply["ok"], case)
            self.assertEqual(reply["reason"], reason, case)

    @unittest.skipUnless(NODE, "需要 node 作为测试夹具（生产用客户端自带运行时）")
    def test_rejects_invalid_payloads(self):
        for case, reason in (("expired_token_field", "TOKEN_MISSING"),
                             ("bad_token_chars", "TOKEN_MISSING"),
                             ("not_object", "UNSUPPORTED_FORMAT"),
                             ("invalid_json", "UNSUPPORTED_FORMAT")):
            process = run_helper(self.blob_file(case), FIXTURE["password"])
            reply = json.loads(process.stdout.decode("utf-8"))
            self.assertFalse(reply["ok"], case)
            self.assertEqual(reply["reason"], reason, case)

    @unittest.skipUnless(NODE, "需要 node 作为测试夹具（生产用客户端自带运行时）")
    def test_password_and_token_never_leak_to_stderr(self):
        process = run_helper(self.blob_file("valid"), "wrong-password-for-leak-check")
        self.assertNotIn(b"wrong-password-for-leak-check", process.stdout)
        self.assertNotIn(b"wrong-password-for-leak-check", process.stderr)
        self.assertNotIn(FIXTURE["cases"]["valid"]["expect_token"].encode(),
                         process.stderr)

    @unittest.skipUnless(NODE, "需要 node 作为测试夹具（生产用客户端自带运行时）")
    def test_rejects_oversized_input(self):
        huge = json.dumps({"version": 1, "password": "p" * 70000, "authFile": "/f"})
        process = subprocess.run([NODE, "-e", qoder.MAC_AUTH_HELPER_JS],
                                 input=huge.encode("utf-8"),
                                 capture_output=True, timeout=30)
        self.assertEqual(json.loads(process.stdout.decode("utf-8"))["reason"],
                         "INVALID_FORMAT")


class KeychainTests(unittest.TestCase):
    def test_reads_password_from_login_keychain(self):
        with mock.patch.object(qoder.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="synthetic-password\n", stderr="")
            self.assertEqual(qoder._mac_keychain_password(), "synthetic-password")
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "security")
        self.assertIn(qoder.MAC_KEYCHAIN_SERVICE, argv)
        self.assertIn(qoder.MAC_KEYCHAIN_ACCOUNT, argv)

    def test_missing_item_is_no_auth(self):
        with mock.patch.object(qoder.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=44, stdout="", stderr="not found")
            with self.assertRaises(qoder.NoAuthError) as ctx:
                qoder._mac_keychain_password()
        self.assertIn(qoder.MAC_KEYCHAIN_SERVICE, str(ctx.exception))

    def test_unavailable_security_command_is_no_auth(self):
        for error in (OSError("missing"), subprocess.TimeoutExpired("security", 10)):
            with mock.patch.object(qoder.subprocess, "run", side_effect=error):
                with self.assertRaises(qoder.NoAuthError):
                    qoder._mac_keychain_password()


class RuntimeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def bundle(self, executable="Qoder CN", create_binary=True):
        app = self.root / "Qoder CN.app" / "Contents"
        (app / "MacOS").mkdir(parents=True, exist_ok=True)
        with open(app / "Info.plist", "wb") as handle:
            plistlib.dump({"CFBundleExecutable": executable}, handle)
        binary = app / "MacOS" / executable
        if create_binary:
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
        return str(self.root / "Qoder CN.app"), binary

    def test_override_requires_executable_file(self):
        # 不用真实的 chmod 断言：Windows 上 os.access(path, os.X_OK) 对任何
        # 存在的文件都为真（没有 POSIX 可执行位），真实语义只有 macOS 才有。
        # 这里 mock 掉 access 的返回值，验证代码确实尊重它，三个平台一致。
        bundle, binary = self.bundle()
        with mock.patch.dict(os.environ, {"QODER_EXE": str(binary)}), \
             mock.patch.object(qoder.os, "access", return_value=False):
            with self.assertRaises(qoder.NoAuthError):
                qoder.find_mac_qoder_runtime()
        with mock.patch.dict(os.environ, {"QODER_EXE": str(binary)}), \
             mock.patch.object(qoder.os, "access", return_value=True):
            self.assertEqual(qoder.find_mac_qoder_runtime(), os.path.abspath(binary))

    def test_info_plist_resolves_executable(self):
        bundle, binary = self.bundle()
        self.assertEqual(qoder._mac_runtime(bundle), str(binary))

    def test_rejects_path_traversal_and_missing_plist(self):
        for name in ("../evil", "a/b", "", "."):
            bundle, _ = self.bundle(executable=name, create_binary=False)
            self.assertIsNone(qoder._mac_runtime(bundle), name)
        self.assertIsNone(qoder._mac_runtime(str(self.root / "absent.app")))

    def test_missing_client_reports_install_hint(self):
        with mock.patch.dict(os.environ, {"QODER_EXE": ""}), \
             mock.patch.object(qoder.os.path, "isfile", return_value=False):
            with self.assertRaises(qoder.NoAuthError) as ctx:
                qoder.find_mac_qoder_runtime()
        self.assertIn("Qoder CN.app", str(ctx.exception))


class AuthDirTests(unittest.TestCase):
    def test_override_wins(self):
        with mock.patch.dict(os.environ, {"QODER_AUTH_DIR": "/tmp/synthetic"}):
            self.assertEqual(qoder.find_mac_auth_dirs(), [Path("/tmp/synthetic")])

    def test_picks_newest_matching_directory(self):
        with mock.patch.dict(os.environ, {"QODER_AUTH_DIR": ""}), \
             mock.patch.object(qoder.glob, "glob", return_value=["/a/com.qodercn.app.stable",
                                                                 "/b/com.qodercn.app.insiders"]), \
             mock.patch.object(qoder.os.path, "getmtime", side_effect=[1.0, 9.0]):
            self.assertEqual(qoder.find_mac_auth_dirs(),
                             [Path("/b/com.qodercn.app.insiders"),
                              Path("/a/com.qodercn.app.stable")])


class LoadSessionMacTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "com.qodercn.app.stable"
        self.root.mkdir()
        (self.root / "auth.v1.dat").write_bytes(b"v10" + b"\x00" * 64)
        patcher = mock.patch.object(qoder.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def load(self, helper, password="synthetic-password"):
        with mock.patch.object(qoder, "find_mac_qoder_runtime", return_value="/fake/Electron"), \
             mock.patch.object(qoder, "find_mac_auth_dirs", return_value=[self.root]), \
             mock.patch.object(qoder, "_mac_keychain_password", return_value=password), \
             mock.patch.object(qoder, "_run_mac_auth_helper", side_effect=helper) as call:
            return qoder._load_session_mac(), call

    def test_returns_session_and_records_auth_dir(self):
        session, call = self.load(lambda exe, request: {"token": "dt-synthetic"})
        self.assertEqual(session["token"], "dt-synthetic")
        self.assertEqual(session["_auth_dir"], str(self.root))
        request = call.call_args[0][1]
        self.assertEqual(request["password"], "synthetic-password")
        self.assertEqual(request["authFile"], str(self.root / "auth.v1.dat"))
        self.assertEqual(call.call_args[0][0], "/fake/Electron")

    def test_no_auth_directory_reports_where_it_looked(self):
        with mock.patch.object(qoder, "find_mac_qoder_runtime", return_value="/fake/Electron"), \
             mock.patch.object(qoder, "find_mac_auth_dirs", return_value=[]):
            with self.assertRaises(qoder.NoAuthError) as ctx:
                qoder._load_session_mac()
        self.assertIn("com.qodercn.app", str(ctx.exception))
        self.assertEqual(ctx.exception.looked_in, [])

    def test_existing_directory_without_credentials_is_no_auth(self):
        (self.root / "auth.v1.dat").unlink()
        with mock.patch.object(qoder, "find_mac_qoder_runtime", return_value="/fake/Electron"), \
             mock.patch.object(qoder, "find_mac_auth_dirs", return_value=[self.root]):
            with self.assertRaises(qoder.NoAuthError) as ctx:
                qoder._load_session_mac()
        self.assertIn("尚未登录", str(ctx.exception))

    def test_helper_failure_is_retried_then_reported(self):
        def boom(exe, request):
            raise qoder.AuthFormatError("DECRYPT_FAILED")
        with self.assertRaises(qoder.AuthFormatError) as ctx:
            self.load(boom)
        self.assertIn("DECRYPT_FAILED", str(ctx.exception))
        self.assertEqual(self.sleep.call_count, 2)

    def test_dispatch_uses_mac_path_on_darwin(self):
        with mock.patch.object(qoder.sys, "platform", "darwin"), \
             mock.patch.object(qoder, "_load_session_mac",
                               return_value={"token": "dt-mac"}) as mac:
            self.assertEqual(qoder.load_session()["token"], "dt-mac")
        mac.assert_called_once()

    def test_dispatch_rejects_other_platforms_with_token_hint(self):
        with mock.patch.object(qoder.sys, "platform", "linux"), \
             mock.patch.object(qoder, "IS_WINDOWS", False):
            with self.assertRaises(qoder.NoAuthError) as ctx:
                qoder.load_session()
        self.assertIn("QODER_TOKEN", str(ctx.exception))


class HelperProtocolTests(unittest.TestCase):
    def reply(self, payload, **kwargs):
        with mock.patch.object(qoder.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout=json.dumps(payload).encode("utf-8"), **kwargs)
            return qoder._run_mac_auth_helper("/fake/Electron",
                                              {"password": "p", "authFile": "/f"}), run

    def test_returns_session(self):
        session, _ = self.reply({"version": 1, "ok": True, "session": {"token": "dt-1"}})
        self.assertEqual(session, {"token": "dt-1"})

    def test_maps_failure_reasons_to_exceptions(self):
        cases = {
            "AUTH_MISSING": qoder.NoAuthError,
            "DECRYPT_FAILED": qoder.AuthFormatError,
            "UNSUPPORTED_FORMAT": qoder.AuthFormatError,
            "TOKEN_MISSING": qoder.AuthFormatError,
            "SOMETHING_NEW": qoder.AuthFormatError,
        }
        for reason, expected in cases.items():
            with self.assertRaises(expected, msg=reason):
                self.reply({"version": 1, "ok": False, "reason": reason})

    def test_rejects_malformed_helper_output(self):
        for payload in ({"version": 2, "ok": True}, {"version": 1, "ok": True, "session": {}},
                        {"version": 1, "ok": True, "session": {"token": 42}}):
            with self.assertRaises(qoder.AuthFormatError, msg=str(payload)):
                self.reply(payload)

    def test_timeout_and_missing_runtime_are_no_auth(self):
        for error in (subprocess.TimeoutExpired("Electron", 20), OSError("gone")):
            with mock.patch.object(qoder.subprocess, "run", side_effect=error):
                with self.assertRaises(qoder.NoAuthError):
                    qoder._run_mac_auth_helper("/fake/Electron", {})

    def test_environment_is_sanitized(self):
        for key in ("QODER_TOKEN", "QODER_EXE", "ELECTRON_NO_ATTACH_CONSOLE", "NODE_OPTIONS"):
            os.environ[key] = "leaked"
            self.addCleanup(os.environ.pop, key, None)
        _, run = self.reply({"version": 1, "ok": True, "session": {"token": "dt-1"}})
        env = run.call_args.kwargs["env"]
        self.assertEqual(env.get("ELECTRON_RUN_AS_NODE"), "1")
        for key in ("QODER_TOKEN", "QODER_EXE", "ELECTRON_NO_ATTACH_CONSOLE", "NODE_OPTIONS"):
            self.assertNotIn(key, env)


if __name__ == "__main__":
    unittest.main()

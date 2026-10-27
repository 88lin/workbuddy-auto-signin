#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macOS 安装/卸载脚本的回归测试（仅 darwin 运行）

覆盖 PR #24 review 指出的四个问题：
  1. 自检用到的 QODER_EXE / QODER_AUTH_DIR 必须写进 plist（launchd 不继承终端环境）
  2. 备份失败必须中止，不能继续覆盖配置并卸载旧任务
  3. 注销失败必须保留配置并返回非零
  4. 路径含 & < > 时 plist 仍必须是合法 XML

做法：把两个脚本复制到名字带「&」的临时目录，用假的 qoder_signin.py 顶替自检，
PATH 里放一个 launchctl 影子程序模拟加载状态与失败分支。不触碰真实用户任务。
"""

import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = "install-qoder-macos.sh"
UNINSTALLER = "uninstall-qoder-macos.sh"

DARWIN_ONLY = unittest.skipUnless(
    sys.platform == "darwin",
    "这些脚本依赖 macOS 的 plutil / launchctl 语义，非 darwin 平台跳过",
)

FAKE_DOCTOR = """\
#!/usr/bin/env python3
import sys
if "doctor" not in sys.argv:
    sys.exit(2)
print('{"result": "OK", "credential_format": "sym-v1"}')
"""

FAKE_LAUNCHCTL = """\
#!/bin/bash
STATE="${SHIM_STATE:?}"
mkdir -p "$STATE"
cmd="${1:-}"
case "$cmd" in
  print)
    [ -f "$STATE/loaded" ] && exit 0 || exit 1
    ;;
  bootout)
    [ "${SHIM_BOOTOUT_FAIL:-0}" = "1" ] && exit 1
    rm -f "$STATE/loaded"
    exit 0
    ;;
  bootstrap)
    [ "${SHIM_BOOTSTRAP_FAIL:-0}" = "1" ] && exit 1
    touch "$STATE/loaded"
    exit 0
    ;;
esac
exit 0
"""


class InstallerHarness(unittest.TestCase):
    """搭一个隔离环境：临时 HOME、带 & 的仓库路径、影子 launchctl"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="qoder-installer-")
        self.addCleanup(shutil.rmtree, self.root, True)

        # 路径里带 " & "，正是评审里提到会破坏 XML 的场景
        self.repo = os.path.join(self.root, "repo & tools")
        os.makedirs(self.repo)
        for name in (INSTALLER, UNINSTALLER):
            src = os.path.join(SCRIPT_DIR, name)
            dst = os.path.join(self.repo, name)
            shutil.copy(src, dst)

        self.script = os.path.join(self.repo, "qoder_signin.py")
        with open(self.script, "w", encoding="utf-8") as handle:
            handle.write(FAKE_DOCTOR)

        self.home = os.path.join(self.root, "home")
        os.makedirs(os.path.join(self.home, "Library", "LaunchAgents"))
        self.plist = os.path.join(self.home, "Library", "LaunchAgents",
                                  "qoder-daily-credits.plist")

        self.shim_dir = os.path.join(self.root, "bin")
        self.state = os.path.join(self.root, "shim-state")
        os.makedirs(self.shim_dir)
        shim = os.path.join(self.shim_dir, "launchctl")
        with open(shim, "w", encoding="utf-8") as handle:
            handle.write(FAKE_LAUNCHCTL)
        os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC)

    def env(self, **extra):
        env = {
            "HOME": self.home,
            "PATH": self.shim_dir + ":/usr/bin:/bin:/usr/sbin:/sbin",
            "SHIM_STATE": self.state,
            "QODER_PYTHON": sys.executable,
            "TMPDIR": self.root,
        }
        env.update(extra)
        return env

    def run_script(self, name, **extra):
        return subprocess.run(
            ["/bin/bash", os.path.join(self.repo, name)],
            cwd=self.repo, env=self.env(**extra),
            capture_output=True, text=True, timeout=120)

    def read_plist(self):
        with open(self.plist, "rb") as handle:
            return plistlib.load(handle)


@DARWIN_ONLY
class InstallerTests(InstallerHarness):
    def test_generates_valid_plist_under_path_with_ampersand(self):
        result = self.run_script(INSTALLER)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = self.read_plist()  # plistlib 解析失败会直接抛异常
        self.assertEqual(data["Label"], "qoder-daily-credits")
        self.assertEqual(os.path.basename(data["ProgramArguments"][1]), "qoder_signin.py")
        self.assertEqual(
            sorted(item["Hour"] for item in data["StartCalendarInterval"]),
            [10, 11, 15, 19, 23])
        self.assertTrue(data["RunAtLoad"])
        self.assertIn("&", data["WorkingDirectory"])

    def test_self_check_env_is_written_into_plist(self):
        # launchd 不继承安装时终端的环境，这两个值必须落到 plist 里
        result = self.run_script(INSTALLER,
                                 QODER_EXE="/tmp/Qoder CN",
                                 QODER_AUTH_DIR="/tmp/auth dir")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        env = self.read_plist()["EnvironmentVariables"]
        self.assertEqual(env.get("QODER_EXE"), "/tmp/Qoder CN")
        self.assertEqual(env.get("QODER_AUTH_DIR"), "/tmp/auth dir")
        self.assertEqual(env.get("PYTHONIOENCODING"), "utf-8")

    def test_no_custom_env_keeps_plist_minimal(self):
        result = self.run_script(INSTALLER)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        env = self.read_plist()["EnvironmentVariables"]
        self.assertEqual(set(env), {"PYTHONIOENCODING"})

    def test_backup_failure_aborts_without_touching_existing_config(self):
        original = b"<plist>ORIGINAL</plist>"
        with open(self.plist, "wb") as handle:
            handle.write(original)
        # 让原配置不可读，cp 备份必然失败（cp 文件→目录 是成功的，不能靠建目录模拟）
        os.chmod(self.plist, 0)

        try:
            result = self.run_script(INSTALLER)
        finally:
            os.chmod(self.plist, 0o600)

        self.assertNotEqual(result.returncode, 0)
        with open(self.plist, "rb") as handle:
            self.assertEqual(handle.read(), original, "备份失败时不应覆盖原配置")

    def test_bootstrap_failure_rolls_back_to_backup(self):
        original = b"<plist>ORIGINAL</plist>"
        with open(self.plist, "wb") as handle:
            handle.write(original)

        result = self.run_script(INSTALLER, SHIM_BOOTSTRAP_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        with open(self.plist, "rb") as handle:
            self.assertEqual(handle.read(), original, "bootstrap 失败后应回滚原配置")

    def test_bootout_failure_aborts_and_restores_backup(self):
        original = b"<plist>ORIGINAL</plist>"
        with open(self.plist, "wb") as handle:
            handle.write(original)
        os.makedirs(self.state, exist_ok=True)
        open(os.path.join(self.state, "loaded"), "w").close()

        result = self.run_script(INSTALLER, SHIM_BOOTOUT_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        with open(self.plist, "rb") as handle:
            self.assertEqual(handle.read(), original, "注销失败时应恢复原配置")


@DARWIN_ONLY
class UninstallerTests(InstallerHarness):
    def write_plist(self, payload=b"<plist>x</plist>"):
        with open(self.plist, "wb") as handle:
            handle.write(payload)

    def test_bootout_failure_keeps_plist_and_fails(self):
        self.write_plist()
        os.makedirs(self.state, exist_ok=True)
        open(os.path.join(self.state, "loaded"), "w").close()

        result = self.run_script(UNINSTALLER, SHIM_BOOTOUT_FAIL="1")
        self.assertNotEqual(result.returncode, 0, "注销失败必须返回非零")
        self.assertTrue(os.path.exists(self.plist), "注销失败时不能删除配置")

    def test_success_removes_plist_and_unloads(self):
        self.write_plist()
        os.makedirs(self.state, exist_ok=True)
        open(os.path.join(self.state, "loaded"), "w").close()

        result = self.run_script(UNINSTALLER)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(self.plist))
        self.assertFalse(os.path.exists(os.path.join(self.state, "loaded")),
                         "服务应处于未加载状态")

    def test_not_loaded_but_plist_present_is_cleaned(self):
        self.write_plist()
        result = self.run_script(UNINSTALLER)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(self.plist))


if __name__ == "__main__":
    unittest.main()

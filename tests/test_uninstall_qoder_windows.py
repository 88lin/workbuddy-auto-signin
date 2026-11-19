"""Run the real Qoder CN uninstaller under a fake scheduler, in isolation.

Mirrors tests/test_install_qoder_windows.py: no ScheduledTasks module is loaded
and no real task is removed. The fake module keeps task existence in an ordered
dictionary so the readback after Unregister-ScheduledTask reflects what the mock
actually did.

Both WorkBuddy tasks are seeded in every scenario because scope is the point of
uninstall-qoder-windows.ps1: it mirrors install-qoder-windows.ps1 and must leave
the WorkBuddy pair alone.
"""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SHELLS = [path for name in ("powershell.exe", "pwsh.exe")
          if (path := shutil.which(name))] if sys.platform == "win32" else []
QODER = "QoderDailyCredits"
FOREIGN = ["\\|WorkBuddyAutoSignin", "\\|WorkBuddyGrowthPoll"]
DONE = "完成：定时任务已全部清除"
LOG = "qoder_signin.log"


HARNESS = r'''
param($Script, $TracePath, $Scenario)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Import-Module Microsoft.PowerShell.Management
Import-Module Microsoft.PowerShell.Utility
# Prevent an omitted mock from falling through to the real scheduler module.
$PSModuleAutoloadingPreference = 'None'
$fake = New-Module -ArgumentList $TracePath, $Scenario -ScriptBlock {
    param($TracePath, $Scenario)
    if ($Scenario -eq 'no-module') {
        # Windows 7 ships no ScheduledTasks component, so export nothing at all:
        # with autoloading off, Get-ScheduledTask stays unreachable exactly as it
        # would be there. New-Module imports the module by itself, which is why
        # skipping the Import-Module further down would not remove the mock.
        return
    }
    $script:ErrorActionPreference = 'Continue'
    $script:TracePath = $TracePath
    $script:Scenario = $Scenario
    $script:Existing = [ordered]@{}
    if ($Scenario -ne 'absent') {
        $script:Existing[('\|QoderDailyCredits')] = $true
    }
    if ($Scenario -eq 'elsewhere') {
        # A same-named task under another path must be reported, never removed.
        $script:Existing[('\Vendor\|QoderDailyCredits')] = $true
    }
    # The neighbour script's tasks, present in every scenario.
    foreach ($n in 'WorkBuddyAutoSignin', 'WorkBuddyGrowthPoll') {
        $script:Existing[('\|' + $n)] = $true
    }
    function Trace($Kind, $Name, $Value) {
        $line = @{kind=$Kind; name=$Name; value=$Value} | ConvertTo-Json -Compress -Depth 10
        [System.IO.File]::AppendAllText($script:TracePath, $line + "`n")
    }
    function Get-ScheduledTask {
        [CmdletBinding()] param($TaskName, $TaskPath)
        if (-not $PSBoundParameters.ContainsKey('TaskName')) {
            $all = @($script:Existing.Keys | ForEach-Object {
                $parts = $_ -split '\|', 2
                [pscustomobject]@{TaskName=$parts[1]; TaskPath=$parts[0]}
            })
            Trace 'scan' '*' @($script:Existing.Keys)
            return $all
        }
        Trace 'get' $TaskName $TaskPath
        if (-not $script:Existing.Contains(($TaskPath + '|' + $TaskName))) { return }
        [pscustomobject]@{TaskName=$TaskName; TaskPath=$TaskPath; State='Ready'}
    }
    # -Confirm is only a common parameter when SupportsShouldProcess is declared,
    # so the mock has to take it explicitly the way the real cmdlet does.
    function Unregister-ScheduledTask {
        [CmdletBinding()] param($TaskName, $TaskPath, [switch]$Confirm)
        Trace 'unregister' $TaskName $TaskPath
        if ($script:Scenario -eq 'denied') {
            Write-Error 'Access is denied (0x80070005)' -ErrorId 'HRESULT 0x80070005,Unregister-ScheduledTask'
            return
        }
        if ($script:Scenario -eq 'phantom') {
            # Returns without error, yet leaves the task in place.
            return
        }
        $script:Existing.Remove(($TaskPath + '|' + $TaskName)) | Out-Null
    }
    Export-ModuleMember -Function Get-ScheduledTask, Unregister-ScheduledTask
}
Import-Module $fake -Force
& $Script
exit $LASTEXITCODE
'''


@unittest.skipUnless(SHELLS, "Windows PowerShell is required for uninstaller tests")
class WindowsQoderUninstallerTests(unittest.TestCase):
    def run_uninstaller(self, shell, scenario, make_log=False,
                        prefix="qoder uninstaller 空格 "):
        with tempfile.TemporaryDirectory(prefix=prefix) as temp:
            root = Path(temp)
            script = root / "uninstall-qoder-windows.ps1"
            shutil.copyfile(ROOT / "uninstall-qoder-windows.ps1", script)
            log = root / LOG
            if make_log:
                log.write_text("[2026-11-19 10:24:34] {}\n", encoding="utf-8")
            harness = root / "harness.ps1"
            harness.write_text(HARNESS, encoding="utf-8-sig")
            trace = root / "trace.jsonl"
            result = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(harness), "-Script", str(script),
                 "-TracePath", str(trace), "-Scenario", scenario],
                capture_output=True, encoding="utf-8", errors="replace", timeout=30)
            events = [json.loads(line) for line in
                      trace.read_text(encoding="utf-8-sig").splitlines()] if trace.exists() else []
            # Snapshot before the context manager deletes the directory.
            return result, events, log.exists()

    def unregisters(self, events):
        return [e["name"] for e in events if e["kind"] == "unregister"]

    def surviving(self, events):
        return events[-1]["value"]

    def assert_refused(self, result):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        # Match the success banner only: the refusal line itself starts with
        # "未完成：", so a looser "完成：" probe would match the failure output.
        self.assertNotIn(DONE, result.stdout)
        self.assertNotIn("已全部清除", result.stdout)

    def test_success_removes_the_task_and_verifies_the_readback(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events, _ = self.run_uninstaller(shell, "success")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(DONE, result.stdout)
                self.assertIn("删除 1 个", result.stdout)
                self.assertIn("install-qoder-windows.ps1", result.stdout)
                self.assertEqual([(e["kind"], e["name"]) for e in events],
                                 [("get", QODER), ("unregister", QODER), ("get", QODER),
                                  ("scan", "*")])
                self.assertEqual(events[1]["value"], "\\")

    def test_never_touches_the_workbuddy_tasks(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events, _ = self.run_uninstaller(shell, "success")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.unregisters(events), [QODER])
                self.assertEqual(self.surviving(events), FOREIGN)

    def test_absent_task_is_skipped_and_still_exits_zero(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events, _ = self.run_uninstaller(shell, "absent")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("未安装，跳过", result.stdout)
                self.assertIn("本就未装 1 个", result.stdout)
                self.assertEqual(self.unregisters(events), [])
                self.assertEqual([e["kind"] for e in events], ["get", "scan"])

    def test_unregister_failure_is_never_reported_as_complete(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events, _ = self.run_uninstaller(shell, "denied")
                self.assert_refused(result)
                self.assertIn("0x80070005", result.stdout)
                self.assertIn("未完成：1 个任务删除失败", result.stdout)
                self.assertIn("可手动执行", result.stdout)
                self.assertIn('-TaskName "QoderDailyCredits"', result.stdout)
                self.assertIn("管理员", result.stdout)
                # Nothing else was removed, so the partial-success note must not appear.
                self.assertNotIn("已删除的（不会恢复", result.stdout)
                self.assertEqual([(e["kind"], e["name"]) for e in events][-2:],
                                 [("unregister", QODER), ("scan", "*")])
                self.assertIn("\\|" + QODER, self.surviving(events))

    def test_phantom_success_is_caught_by_readback(self):
        # The dangerous case: the cmdlet returns clean, the scheduler keeps the
        # task, and a script that trusts the return value would say "done".
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events, _ = self.run_uninstaller(shell, "phantom")
                self.assert_refused(result)
                self.assertIn("命令返回成功但任务仍在", result.stdout)
                self.assertIn("可手动执行", result.stdout)
                self.assertEqual(self.unregisters(events), [QODER])
                self.assertIn("\\|" + QODER, self.surviving(events))
                self.assertEqual(self.surviving(events)[:1] + FOREIGN,
                                 ["\\|" + QODER] + FOREIGN)

    def test_elsewhere_task_is_warned_about_but_not_removed(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events, _ = self.run_uninstaller(shell, "elsewhere")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("以下位置还有同名任务", result.stdout)
                self.assertIn("\\Vendor\\" + QODER, result.stdout)
                self.assertEqual(self.unregisters(events), [QODER])
                self.assertIn("\\Vendor\\|" + QODER, self.surviving(events))

    def test_missing_scheduler_component_refuses_before_touching_anything(self):
        # Windows 7 ships no ScheduledTasks module, so the guard has to speak
        # instead of letting the first Get-ScheduledTask die on
        # CommandNotFoundException — which, verified against a real session with
        # autoloading off, -ErrorAction SilentlyContinue cannot suppress.
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events, _ = self.run_uninstaller(shell, "no-module")
                self.assert_refused(result)
                self.assertIn("ScheduledTasks", result.stdout)
                self.assertIn("Windows 8", result.stdout)
                self.assertIn("install-qoder-windows.ps1", result.stdout)
                self.assertEqual(events, [])

    def test_reports_the_environment_it_run_on(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, _, _ = self.run_uninstaller(shell, "success")
                self.assertIn("系统 Windows ", result.stdout)
                self.assertIn("PowerShell ", result.stdout)

    def test_log_is_reported_but_never_deleted(self):
        for shell in SHELLS:
            with self.subTest(shell=shell, log="present"):
                result, _, exists = self.run_uninstaller(shell, "success", make_log=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(exists)
                self.assertIn("保留", result.stdout)
                self.assertIn("Remove-Item -LiteralPath", result.stdout)

            with self.subTest(shell=shell, log="missing"):
                result, _, exists = self.run_uninstaller(shell, "success", make_log=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(exists)
                self.assertIn("还没有日志文件", result.stdout)

    def test_bracketed_directory_resolves_literal_paths(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events, exists = self.run_uninstaller(
                    shell, "success", make_log=True,
                    prefix="qoder uninstaller [brackets] ")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("[brackets]", result.stdout)
                self.assertTrue(exists)
                self.assertEqual(self.unregisters(events), [QODER])


if __name__ == "__main__":
    unittest.main()

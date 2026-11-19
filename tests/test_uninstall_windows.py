"""Run the real WorkBuddy uninstaller under a fake scheduler, in isolation.

Mirrors tests/test_install_windows.py: no ScheduledTasks module is loaded and no
real task is removed. The fake module keeps task existence in an ordered
dictionary so the readback after Unregister-ScheduledTask reflects what the mock
actually did. That is the only way to exercise the two outcomes the uninstaller
must refuse to report as complete: a cmdlet that raises, and a cmdlet that
claims success while leaving the task in place.

QoderDailyCredits is seeded in every scenario because the whole point of
uninstall-windows.ps1 is its scope: it mirrors install-windows.ps1 and must not
touch the task install-qoder-windows.ps1 owns.
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
DAILY = "WorkBuddyAutoSignin"
POLL = "WorkBuddyGrowthPoll"
FOREIGN = "QoderDailyCredits"
OWN = (DAILY, POLL)
DONE = "完成：定时任务已全部清除"
LOG = "signin.log"


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
        foreach ($n in 'WorkBuddyAutoSignin', 'WorkBuddyGrowthPoll') {
            $script:Existing[('\|' + $n)] = $true
        }
    }
    if ($Scenario -eq 'elsewhere') {
        # A same-named task under another path must be reported, never removed.
        $script:Existing[('\Vendor\|WorkBuddyGrowthPoll')] = $true
    }
    # The neighbour script's task, present in every scenario.
    $script:Existing[('\|QoderDailyCredits')] = $true
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
        if ($script:Scenario -eq 'denied' -and $TaskName -eq 'WorkBuddyGrowthPoll') {
            Write-Error 'Access is denied (0x80070005)' -ErrorId 'HRESULT 0x80070005,Unregister-ScheduledTask'
            return
        }
        if ($script:Scenario -eq 'phantom' -and $TaskName -eq 'WorkBuddyAutoSignin') {
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
class WindowsUninstallerTests(unittest.TestCase):
    def run_uninstaller(self, shell, scenario, make_log=False,
                        prefix="workbuddy uninstaller 空格 "):
        with tempfile.TemporaryDirectory(prefix=prefix) as temp:
            root = Path(temp)
            script = root / "uninstall-windows.ps1"
            shutil.copyfile(ROOT / "uninstall-windows.ps1", script)
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

    def test_success_removes_both_and_verifies_each_readback(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events, _ = self.run_uninstaller(shell, "success")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(DONE, result.stdout)
                self.assertIn("删除 2 个", result.stdout)
                # Every removal must be followed by its own confirming read.
                self.assertEqual([(e["kind"], e["name"]) for e in events],
                                 [("get", DAILY), ("unregister", DAILY), ("get", DAILY),
                                  ("get", POLL), ("unregister", POLL), ("get", POLL),
                                  ("scan", "*")])
                self.assertEqual([e["value"] for e in events
                                  if e["kind"] == "unregister"], ["\\", "\\"])

    def test_never_touches_the_qoder_task(self):
        # Scope is the reason this script exists next to its Qoder twin.
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events, _ = self.run_uninstaller(shell, "success")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn(FOREIGN, self.unregisters(events))
                self.assertEqual(self.surviving(events), ["\\|" + FOREIGN])
                self.assertNotIn(FOREIGN, result.stdout)

    def test_absent_tasks_are_skipped_and_still_exit_zero(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events, _ = self.run_uninstaller(shell, "absent")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("本就未装 2 个", result.stdout)
                self.assertEqual(self.unregisters(events), [])
                self.assertEqual([e["kind"] for e in events], ["get", "get", "scan"])

    def test_unregister_failure_is_never_reported_as_complete(self):
        # The other task really did go, and that must not buy a success banner.
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events, _ = self.run_uninstaller(shell, "denied")
                self.assert_refused(result)
                self.assertIn("0x80070005", result.stdout)
                self.assertIn("未完成：1 个任务删除失败", result.stdout)
                self.assertIn("可手动执行", result.stdout)
                self.assertIn('-TaskName "WorkBuddyGrowthPoll"', result.stdout)
                self.assertIn("管理员", result.stdout)
                self.assertIn("已删除的（不会恢复", result.stdout)
                self.assertEqual(self.unregisters(events), [DAILY, POLL])
                # The failing branch continues before its readback.
                self.assertEqual([(e["kind"], e["name"]) for e in events][-2:],
                                 [("unregister", POLL), ("scan", "*")])

    def test_phantom_success_is_caught_by_readback(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events, _ = self.run_uninstaller(shell, "phantom")
                self.assert_refused(result)
                self.assertIn("命令返回成功但任务仍在", result.stdout)
                self.assertIn("可手动执行", result.stdout)
                self.assertEqual(self.unregisters(events), [DAILY, POLL])
                self.assertIn("\\|" + DAILY, self.surviving(events))
                # The neighbour task still survives the lying branch too.
                self.assertIn("\\|" + FOREIGN, self.surviving(events))

    def test_elsewhere_task_is_warned_about_but_not_removed(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events, _ = self.run_uninstaller(shell, "elsewhere")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("\\Vendor\\" + POLL, result.stdout)
                self.assertEqual(self.unregisters(events), list(OWN))
                self.assertIn("\\Vendor\\|" + POLL, self.surviving(events))

    def test_missing_scheduler_component_refuses_before_touching_anything(self):
        # Windows 7 ships no ScheduledTasks module. The guard must say that in
        # plain words instead of letting the first Get-ScheduledTask die on
        # CommandNotFoundException, which -ErrorAction SilentlyContinue cannot
        # suppress (verified against a real session with autoloading off).
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events, _ = self.run_uninstaller(shell, "no-module")
                self.assert_refused(result)
                self.assertIn("ScheduledTasks", result.stdout)
                self.assertIn("Windows 8", result.stdout)
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
                self.assertIn(LOG, result.stdout)

            with self.subTest(shell=shell, log="missing"):
                result, _, exists = self.run_uninstaller(shell, "success", make_log=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(exists)
                self.assertIn("还没有日志文件", result.stdout)

    def test_bracketed_directory_resolves_literal_paths(self):
        # $PSScriptRoot must survive brackets and spaces, or the log lookup would
        # target a wildcard-expanded path.
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events, exists = self.run_uninstaller(
                    shell, "success", make_log=True,
                    prefix="workbuddy uninstaller [brackets] ")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("[brackets]", result.stdout)
                self.assertTrue(exists)
                self.assertEqual(self.unregisters(events), list(OWN))


if __name__ == "__main__":
    unittest.main()

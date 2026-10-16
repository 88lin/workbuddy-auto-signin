"""Run the real installer in isolated PowerShell processes with a fake scheduler.

No ScheduledTasks module is loaded and no real task or Python process is started.
The fake module has its own Continue preference, matching the module-scope error
behavior that made the installer's script-level Stop preference insufficient.
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

HARNESS = r'''
param($Installer, $TracePath, $Scenario)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Import-Module Microsoft.PowerShell.Management
Import-Module Microsoft.PowerShell.Utility
# Prevent an omitted mock from falling through to the real scheduler module.
$PSModuleAutoloadingPreference = 'None'
$fake = New-Module -ArgumentList $TracePath, $Scenario, (Split-Path $Installer) -ScriptBlock {
    param($TracePath, $Scenario, $FixtureRoot)
    $script:ErrorActionPreference = 'Continue'
    $script:TracePath = $TracePath
    $script:Scenario = $Scenario
    $script:FixtureRoot = $FixtureRoot
    function Trace($Kind, $Name, $Value) {
        $line = @{kind=$Kind; name=$Name; value=$Value} | ConvertTo-Json -Compress -Depth 10
        [System.IO.File]::AppendAllText($script:TracePath, $line + "`n")
    }
    function Get-Command {
        [CmdletBinding()] param($Name)
        if ($Name -ne 'pythonw') { throw "Unexpected command lookup: $Name" }
        [pscustomobject]@{Source=(Join-Path $script:FixtureRoot 'pythonw.exe')}
    }
    function New-ScheduledTaskPrincipal {
        [CmdletBinding()] param($UserId, $LogonType, $RunLevel)
        if ($script:Scenario -eq 'principal-error') {
            Write-Error 'Principal creation failed'
            return
        }
        [pscustomobject]@{UserId=$UserId; LogonType=$LogonType; RunLevel=$RunLevel}
    }
    function New-ScheduledTaskAction {
        [CmdletBinding()] param($Execute, $Argument)
        [pscustomobject]@{Execute=$Execute; Arguments=$Argument}
    }
    function New-ScheduledTaskTrigger {
        [CmdletBinding()] param([switch]$Daily, $At)
        [pscustomobject]@{Daily=[bool]$Daily; At=$At}
    }
    function New-ScheduledTaskSettingsSet {
        [CmdletBinding()] param([switch]$StartWhenAvailable, [switch]$Hidden,
            [switch]$DontStopIfGoingOnBatteries, [switch]$AllowStartIfOnBatteries,
            [timespan]$ExecutionTimeLimit)
        [pscustomobject]@{Seconds=$ExecutionTimeLimit.TotalSeconds; Hidden=[bool]$Hidden;
            StartWhenAvailable=[bool]$StartWhenAvailable}
    }
    function Register-ScheduledTask {
        [CmdletBinding()] param($TaskName, $TaskPath, $Action, $Trigger, $Settings,
            $Principal, $Description, [switch]$Force)
        Trace 'register' $TaskName @{path=$TaskPath; action=$Action; triggers=$Trigger;
            settings=$Settings; principal=$Principal; force=[bool]$Force}
        if (($script:Scenario -eq 'register-first' -and $TaskName -eq 'WorkBuddyAutoSignin') -or
            ($script:Scenario -eq 'register-second' -and $TaskName -eq 'WorkBuddyGrowthPoll')) {
            Write-Error 'Access is denied (0x80070005)' -ErrorId 'HRESULT 0x80070005,Register-ScheduledTask'
            return
        }
        [pscustomobject]@{TaskName=$TaskName; State='Ready'}
    }
    function Get-ScheduledTask {
        [CmdletBinding()] param($TaskName, $TaskPath)
        Trace 'get' $TaskName $TaskPath
        if ($script:Scenario -eq 'get-error' -and $TaskName -eq 'WorkBuddyGrowthPoll') {
            Write-Error 'Cannot read scheduled task'
            return
        }
        if ($script:Scenario -eq ('missing-' + $TaskName)) { return }
        [pscustomobject]@{TaskName=$TaskName; TaskPath=$TaskPath; State='Ready'}
    }
    function Get-ScheduledTaskInfo {
        [CmdletBinding()] param($TaskName, $TaskPath)
        Trace 'info' $TaskName $TaskPath
        if ($TaskName -eq 'WorkBuddyGrowthPoll') {
            if ($script:Scenario -eq 'info-error') {
                Write-Error 'Cannot read scheduled task info'
                return
            }
            if ($script:Scenario -eq 'info-missing') { return }
        }
        [pscustomobject]@{NextRunTime=[datetime]'2026-10-17T00:05:00'}
    }
    Export-ModuleMember -Function Get-Command, New-ScheduledTaskPrincipal,
        New-ScheduledTaskAction, New-ScheduledTaskTrigger, New-ScheduledTaskSettingsSet,
        Register-ScheduledTask, Get-ScheduledTask, Get-ScheduledTaskInfo
}
Import-Module $fake -Force
& $Installer
exit $LASTEXITCODE
'''


@unittest.skipUnless(SHELLS, "Windows PowerShell is required for installer tests")
class WindowsInstallerTests(unittest.TestCase):
    def run_installer(self, shell, scenario):
        with tempfile.TemporaryDirectory(prefix="workbuddy installer 空格 ") as temp:
            root = Path(temp)
            installer = root / "install-windows.ps1"
            shutil.copyfile(ROOT / "install-windows.ps1", installer)
            (root / "signin.py").touch()
            (root / "pythonw.exe").touch()
            harness = root / "harness.ps1"
            harness.write_text(HARNESS, encoding="utf-8-sig")
            trace = root / "trace.jsonl"
            result = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(harness), "-Installer", str(installer),
                 "-TracePath", str(trace), "-Scenario", scenario],
                capture_output=True, encoding="utf-8", errors="replace", timeout=30)
            events = [json.loads(line) for line in trace.read_text(encoding="utf-8-sig").splitlines()] \
                if trace.exists() else []
            return result, events

    def assert_failed(self, result):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("两个定时任务已就位", result.stdout)
        self.assertNotIn(" 完成", result.stdout)
        self.assertIn("失败", result.stdout)

    def test_success_verifies_both_tasks_and_preserves_schedules(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events = self.run_installer(shell, "success")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("两个定时任务已就位", result.stdout)
                self.assertEqual([(e["kind"], e["name"]) for e in events],
                                 [("register", DAILY), ("register", POLL),
                                  ("get", DAILY), ("info", DAILY),
                                  ("get", POLL), ("info", POLL)])
                for event, mode, seconds in zip(events[:2], ("silent", "silent-poll"), (600, 300)):
                    value = event["value"]
                    self.assertEqual(value["path"], "\\")
                    self.assertTrue(value["force"])
                    self.assertEqual(value["principal"]["RunLevel"], "Limited")
                    self.assertEqual(value["principal"]["LogonType"], "Interactive")
                    self.assertEqual(value["settings"]["Seconds"], seconds)
                    self.assertTrue(value["settings"]["StartWhenAvailable"])
                    self.assertTrue(value["settings"]["Hidden"])
                    self.assertRegex(value["action"]["Arguments"], '^".*signin[.]py" ' + mode + '$')
                self.assertEqual(events[0]["value"]["triggers"]["At"], "00:05")
                self.assertEqual([t["At"] for t in events[1]["value"]["triggers"]],
                                 ["01:00", "05:00", "09:00", "13:00", "17:00", "21:00"])
                self.assertTrue(all(e["value"] == "\\" for e in events[2:]))

    def test_registration_error_stops_even_when_old_tasks_can_be_read(self):
        # The mock reads return Ready even after registration fails: existing tasks
        # must not make a failed update look like a successful installation.
        for shell in SHELLS:
            for scenario, names in (("register-first", [DAILY]), ("register-second", [DAILY, POLL])):
                with self.subTest(shell=shell, scenario=scenario):
                    result, events = self.run_installer(shell, scenario)
                    self.assert_failed(result)
                    self.assertEqual([(e["kind"], e["name"]) for e in events],
                                     [("register", name) for name in names])
                    self.assertIn("0x80070005", result.stdout)
                    self.assertIn("管理员", result.stdout)
                    self.assertIn("模式 A", result.stdout)
                    if scenario == "register-second":
                        self.assertIn("已注册：" + DAILY, result.stdout)

    def test_readback_failures_never_print_success(self):
        for shell in SHELLS:
            for scenario in ("get-error", "missing-" + DAILY, "missing-" + POLL,
                             "info-error", "info-missing"):
                with self.subTest(shell=shell, scenario=scenario):
                    result, events = self.run_installer(shell, scenario)
                    self.assert_failed(result)
                    self.assertEqual([e["name"] for e in events if e["kind"] == "register"], [DAILY, POLL])
                    self.assertIn("校验", result.stdout)

    def test_definition_failure_stops_before_registration(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events = self.run_installer(shell, "principal-error")
                self.assert_failed(result)
                self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()

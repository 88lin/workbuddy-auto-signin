"""Run the Qoder installer in isolated PowerShell processes with a fake scheduler.

The fake ScheduledTasks module lets these tests verify registration, readback,
and failure handling without touching the real task scheduler or running Python.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SHELLS = [path for name in ("powershell.exe", "pwsh.exe")
          if (path := shutil.which(name))] if sys.platform == "win32" else []
TASK = "QoderDailyCredits"


def current_user_identity(shell):
    script = ("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
              "[System.Security.Principal.WindowsIdentity]::GetCurrent().Name")
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    return result.stdout.strip()


HARNESS = r'''
param($Installer, $TracePath, $Scenario)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Import-Module Microsoft.PowerShell.Management
Import-Module Microsoft.PowerShell.Utility
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
        Trace 'register' $TaskName @{path=$TaskPath; action=$Action; triggers=@($Trigger);
            settings=$Settings; principal=$Principal; force=[bool]$Force}
        if ($script:Scenario -eq 'register-error') {
            Write-Error 'Access is denied (0x80070005)' -ErrorId 'HRESULT 0x80070005,Register-ScheduledTask'
            return
        }
        [pscustomobject]@{TaskName=$TaskName; State='Ready'}
    }
    function Get-ScheduledTask {
        [CmdletBinding()] param($TaskName, $TaskPath)
        Trace 'get' $TaskName $TaskPath
        if ($script:Scenario -eq 'get-error') {
            Write-Error 'Cannot read scheduled task'
            return
        }
        if ($script:Scenario -eq 'missing-task') { return }
        [pscustomobject]@{TaskName=$TaskName; TaskPath=$TaskPath; State='Ready'}
    }
    function Get-ScheduledTaskInfo {
        [CmdletBinding()] param($TaskName, $TaskPath)
        Trace 'info' $TaskName $TaskPath
        if ($script:Scenario -eq 'info-error') {
            Write-Error 'Cannot read scheduled task info'
            return
        }
        if ($script:Scenario -eq 'info-missing') { return }
        [pscustomobject]@{NextRunTime=[datetime]'2026-10-21T10:05:00'}
    }
    Export-ModuleMember -Function Get-Command, New-ScheduledTaskPrincipal,
        New-ScheduledTaskAction, New-ScheduledTaskTrigger, New-ScheduledTaskSettingsSet,
        Register-ScheduledTask, Get-ScheduledTask, Get-ScheduledTaskInfo
}
Import-Module $fake -Force
& $Installer
exit $LASTEXITCODE
'''

def expected_times():
    now = datetime.now(timezone.utc)
    reset = datetime(now.year, now.month, now.day, 2, 0, tzinfo=timezone.utc)
    primary = reset.astimezone().replace(tzinfo=None) + timedelta(minutes=5)
    return [(primary + timedelta(hours=h)).strftime("%H:%M") for h in (0, 1, 5, 9, 13)]


@unittest.skipUnless(SHELLS, "Windows PowerShell is required for installer tests")
class QoderWindowsInstallerTests(unittest.TestCase):
    def run_installer(self, shell, scenario, prefix="qoder installer 空格 ", neutral_cwd=False):
        with tempfile.TemporaryDirectory(prefix=prefix) as temp:
            root = Path(temp)
            installer = root / "install-qoder-windows.ps1"
            shutil.copyfile(ROOT / "install-qoder-windows.ps1", installer)
            (root / "qoder_signin.py").touch()
            (root / "pythonw.exe").touch()
            harness = root / "harness.ps1"
            harness.write_text(HARNESS, encoding="utf-8-sig")
            trace = root / "trace.jsonl"
            cwd = None
            if neutral_cwd:
                cwd = root / "cwd"
                cwd.mkdir()
            result = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(harness), "-Installer", str(installer),
                 "-TracePath", str(trace), "-Scenario", scenario],
                cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
            events = [json.loads(line) for line in
                      trace.read_text(encoding="utf-8-sig").splitlines()] \
                if trace.exists() else []
            return result, events

    def assert_failed(self, result):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("定时任务已就位", result.stdout)
        self.assertIn("失败", result.stdout)

    def test_success_registers_and_verifies_task(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                identity = current_user_identity(shell)
                result, events = self.run_installer(shell, "success")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("定时任务已就位", result.stdout)
                self.assertIn("下次运行", result.stdout)
                self.assertEqual([(e["kind"], e["name"]) for e in events],
                                 [("register", TASK), ("get", TASK), ("info", TASK)])
                value = events[0]["value"]
                self.assertEqual(value["path"], "\\")
                self.assertTrue(value["force"])
                self.assertEqual(value["principal"]["RunLevel"], "Limited")
                self.assertEqual(value["principal"]["LogonType"], "Interactive")
                self.assertEqual(value["principal"]["UserId"], identity)
                self.assertEqual(value["settings"]["Seconds"], 300)
                self.assertTrue(value["settings"]["StartWhenAvailable"])
                self.assertTrue(value["settings"]["Hidden"])
                self.assertRegex(value["action"]["Arguments"],
                                 '^".*qoder_signin[.]py" silent$')
                self.assertEqual([t["At"] for t in value["triggers"]], expected_times())
                self.assertTrue(all(e["value"] == "\\" for e in events[1:]))

    def test_bracketed_install_directory_resolves_literal_paths(self):
        # A neutral cwd is required: when $PSScriptRoot lookup breaks, the installer
        # falls back to Get-Location, where the repo's real qoder_signin.py would mask it.
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events = self.run_installer(
                    shell, "success",
                    prefix="qoder installer [brackets] ",
                    neutral_cwd=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("定时任务已就位", result.stdout)
                self.assertEqual([(e["kind"], e["name"]) for e in events],
                                 [("register", TASK), ("get", TASK), ("info", TASK)])
                action = events[0]["value"]["action"]
                self.assertIn("[brackets]", action["Execute"])
                self.assertIn("[brackets]", action["Arguments"])
                self.assertTrue(action["Arguments"].endswith('qoder_signin.py" silent'))

    def test_definition_failure_stops_before_registration(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events = self.run_installer(shell, "principal-error")
                self.assert_failed(result)
                self.assertEqual(events, [])

    def test_registration_error_stops_even_when_old_task_can_be_read(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result, events = self.run_installer(shell, "register-error")
                self.assert_failed(result)
                self.assertEqual([(e["kind"], e["name"]) for e in events],
                                 [("register", TASK)])
                self.assertIn("0x80070005", result.stdout)
                self.assertIn("管理员", result.stdout)

    def test_readback_failures_never_print_success(self):
        for shell in SHELLS:
            for scenario in ("get-error", "missing-task", "info-error", "info-missing"):
                with self.subTest(shell=shell, scenario=scenario):
                    result, events = self.run_installer(shell, scenario)
                    self.assert_failed(result)
                    expected = ["register", "get", "info"] \
                        if scenario.startswith("info-") else ["register", "get"]
                    self.assertEqual([e["kind"] for e in events], expected)
                    self.assertIn("校验", result.stdout)


if __name__ == "__main__":
    unittest.main()

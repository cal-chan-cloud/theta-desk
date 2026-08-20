# Register the Theta Desk daily refresh with Task Scheduler.
#
#   powershell -ExecutionPolicy Bypass -File register_task.ps1
#
# Three settings here are not optional, each learned from a task that looked
# registered and then silently never ran:
#
#  1. The action must be  cmd /c ""<full path to .bat>""  with DOUBLED inner
#     quotes.  A bare path containing a space is split by the scheduler and the
#     task dies with 0x800700C1 ("not a valid Win32 application").
#  2. Battery flags OFF.  With the defaults, Windows sends the equivalent of a
#     Control-C to a running task the moment the machine goes on battery, and
#     the run dies half-way through with a misleading exit code.
#  3. StartWhenAvailable ON, so a run missed while the machine was asleep is
#     picked up rather than skipped until tomorrow.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$bat = Join-Path $root "run_daily.bat"
$taskName = "ThetaDesk-DailyRefresh"

if (-not (Test-Path $bat)) { throw "run_daily.bat not found at $bat" }

# Doubled inner quotes -- see note 1 above.
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$bat`"`"" -WorkingDirectory $root

# 07:10 ET is after the option chains refresh for the new session but well
# before the open, so the board is ready when you sit down.
#
# The trigger REPEATS every 4 hours for 12 hours, and that is not about
# intraday freshness -- it is a catch-up.  On 2026-08-20 the machine was asleep
# at 07:10; StartWhenAvailable did not fire it on wake and Task Scheduler
# simply advanced NextRunTime to the following day, leaving the desk a full
# session stale with no error anywhere.  A repeating trigger removes that
# single point of failure: any later repetition picks the day up.
#
# Safe to repeat because the pipeline is idempotent -- it replaces the current
# day's board rather than appending -- and MultipleInstances IgnoreNew means a
# repetition that lands while a run is still going is skipped, not queued.
$trigger = New-ScheduledTaskTrigger -Daily -At 7:10AM
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At 7:10AM `
    -RepetitionInterval (New-TimeSpan -Hours 4) `
    -RepetitionDuration (New-TimeSpan -Hours 12)).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)

try { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false } catch { }

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Theta Desk: refresh option chains, prices, news and the daily trade board." | Out-Null

Write-Output "Registered '$taskName' -- daily at 07:10."
Write-Output ""
Write-Output "Verify with:"
Write-Output "  Get-ScheduledTaskInfo -TaskName '$taskName'"
Write-Output "Run it once now with:"
Write-Output "  Start-ScheduledTask -TaskName '$taskName'"
Write-Output ""
Write-Output "LastTaskResult=0 is meaningful here: pipeline.py re-reads the database"
Write-Output "afterwards and exits non-zero unless the data is genuinely present."
Write-Output ""
Write-Output "To autostart the site at logon (needs an elevated shell):"
Write-Output "  `$a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c `"`"$root\run_site.bat`"`"'"
Write-Output "  Register-ScheduledTask -TaskName 'ThetaDesk-Site' -Action `$a ``"
Write-Output "      -Trigger (New-ScheduledTaskTrigger -AtLogOn) -RunLevel Highest"

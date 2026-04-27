@echo off
REM register-task.bat -- (re)register the ClaudeExecutor scheduled task so
REM start.bat launches automatically at user logon. Idempotent: removes the
REM existing task first if present, then re-creates it.
REM
REM Usage: just double-click. No admin required (the task runs as the
REM current interactive user).
REM
REM Implementation note: schtasks.exe /Create returns "access denied" in
REM some non-interactive sessions. We use PowerShell's Register-ScheduledTask
REM cmdlet instead, which works without elevation for current-user tasks.

setlocal

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "TASK_NAME=ClaudeExecutor"
set "TARGET=%SCRIPT_DIR%\start.bat"

if not exist "%TARGET%" (
  echo [error] start.bat not found at %TARGET%
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$user='%USERDOMAIN%\%USERNAME%';" ^
  "$action  = New-ScheduledTaskAction  -Execute '%TARGET%';" ^
  "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user;" ^
  "$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive;" ^
  "$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Days 0);" ^
  "Unregister-ScheduledTask -TaskName '%TASK_NAME%' -Confirm:$false -ErrorAction SilentlyContinue | Out-Null;" ^
  "Register-ScheduledTask  -TaskName '%TASK_NAME%' -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null;" ^
  "Write-Host ('Registered ' + '%TASK_NAME%' + ' (logon trigger for ' + $user + ')');"
if errorlevel 1 (
  echo [error] PowerShell registration failed.
  exit /b 1
)

echo.
echo To start it now without logging out:
echo   schtasks /Run /TN "%TASK_NAME%"
echo To remove it later:
echo   powershell -Command "Unregister-ScheduledTask -TaskName '%TASK_NAME%' -Confirm:$false"
endlocal

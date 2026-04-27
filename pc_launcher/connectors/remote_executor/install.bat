@echo off
setlocal EnableDelayedExpansion

echo === remote_executor (codex) installer ===
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [error] python not found on PATH. Install Python 3.11+ first.
  exit /b 1
)

where codex >nul 2>nul
if errorlevel 1 (
  echo [warn]  codex CLI not found on PATH.
  echo         The executor still starts but turn.start commands will fail
  echo         until codex is installed: npm install -g @openai/codex
  echo.
)

REM Resolve paths. This script lives at
REM   pc_launcher/connectors/remote_executor/install.bat
REM Walk up to ops-cure root and to the launcher root.
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "LAUNCHER_DIR=%SCRIPT_DIR%\..\.."
set "PROJECT_FILE=%LAUNCHER_DIR%\projects\remote_executor\project.yaml"
set "ENV_FILE=%LAUNCHER_DIR%\.env"

if not exist "%PROJECT_FILE%" (
  echo [error] project.yaml not found at %PROJECT_FILE%
  echo         The repo layout looks wrong -- did you copy the whole
  echo         pc_launcher/ tree?
  exit /b 1
)

REM ---- bridge config -------------------------------------------------------
echo --- bridge config ---
echo.
echo The bridge URL is read from project.yaml (bridge.base_url). The token
echo is read from %ENV_FILE% as BRIDGE_TOKEN. We'll inspect / set both.
echo.

REM Read existing base_url from project.yaml so we can show the default.
for /f "tokens=2 delims=: " %%U in ('findstr /R "^  *base_url:" "%PROJECT_FILE%"') do set "EXISTING_URL=%%U"
if defined EXISTING_URL set "EXISTING_URL=!EXISTING_URL: =!"

if defined EXISTING_URL (
  echo Current bridge.base_url in project.yaml: !EXISTING_URL!
  set /p BRIDGE_URL="Bridge base URL [!EXISTING_URL!]: "
  if "!BRIDGE_URL!"=="" set "BRIDGE_URL=!EXISTING_URL!"
) else (
  set /p BRIDGE_URL="Bridge base URL (e.g. http://semirainnas.tailb6e1a3.ts.net:18080): "
)

if "!BRIDGE_URL!"=="" (
  echo [error] bridge URL is required.
  exit /b 1
)

REM Patch project.yaml's bridge.base_url line. PowerShell does the regex.
powershell -NoProfile -Command "(Get-Content '%PROJECT_FILE%') -replace '^(\s*base_url:\s*).*', '${1}!BRIDGE_URL!' | Set-Content '%PROJECT_FILE%'"
echo Updated bridge.base_url in project.yaml -> !BRIDGE_URL!

REM ---- token: write into pc_launcher/.env ----------------------------------
if exist "%ENV_FILE%" (
  echo Existing %ENV_FILE% found.
  for /f "usebackq tokens=1,* delims==" %%a in ("%ENV_FILE%") do (
    if /i "%%a"=="BRIDGE_TOKEN" set "EXISTING_TOKEN=%%b"
  )
  if defined EXISTING_TOKEN (
    echo BRIDGE_TOKEN already set ^(****^). Press Enter to keep, or paste a new value.
    set /p BRIDGE_TOKEN="Bridge bearer token [keep existing]: "
    if "!BRIDGE_TOKEN!"=="" set "BRIDGE_TOKEN=!EXISTING_TOKEN!"
  ) else (
    set /p BRIDGE_TOKEN="Bridge bearer token: "
  )
) else (
  set /p BRIDGE_TOKEN="Bridge bearer token: "
)

if "!BRIDGE_TOKEN!"=="" (
  echo [error] BRIDGE_TOKEN is required.
  exit /b 1
)

REM Rewrite .env preserving any other keys, replacing or appending BRIDGE_TOKEN.
powershell -NoProfile -Command "$path='%ENV_FILE%'; $token='!BRIDGE_TOKEN!'; if (Test-Path $path) { $lines = Get-Content $path; $found=$false; $out = foreach ($l in $lines) { if ($l -match '^BRIDGE_TOKEN=') { $found=$true; \"BRIDGE_TOKEN=$token\" } else { $l } }; if (-not $found) { $out = $out + \"BRIDGE_TOKEN=$token\" } } else { $out = @(\"BRIDGE_TOKEN=$token\") }; Set-Content -Path $path -Value $out -Encoding utf8"
echo Wrote BRIDGE_TOKEN to %ENV_FILE%

REM ---- machine id (optional override) --------------------------------------
echo.
echo --- machine id ---
echo Default = this PC's hostname (lowercased). Override only if you want a
echo different machineId in the bridge / browser sidebar.
set "MACHINE_ID=%COMPUTERNAME%"
set /p USER_MACHINE_ID="Machine id [%MACHINE_ID%]: "
if not "%USER_MACHINE_ID%"=="" (
  set "MACHINE_ID=%USER_MACHINE_ID%"
  REM start.bat reads REMOTE_EXECUTOR_MACHINE_ID from env if present;
  REM persist it into pc_launcher/.env so Task Scheduler launches see it.
  powershell -NoProfile -Command "$path='%ENV_FILE%'; $val='!MACHINE_ID!'; $lines = Get-Content $path; $found=$false; $out = foreach ($l in $lines) { if ($l -match '^REMOTE_EXECUTOR_MACHINE_ID=') { $found=$true; \"REMOTE_EXECUTOR_MACHINE_ID=$val\" } else { $l } }; if (-not $found) { $out = $out + \"REMOTE_EXECUTOR_MACHINE_ID=$val\" }; Set-Content -Path $path -Value $out -Encoding utf8"
  echo Wrote REMOTE_EXECUTOR_MACHINE_ID=!MACHINE_ID! to %ENV_FILE%
)

echo.
echo --- next steps ---
echo  1. Test once:    %SCRIPT_DIR%\start.bat
echo  2. Autostart on logon:    %SCRIPT_DIR%\register-task.bat
echo  3. Verify in the browser sidebar -- the machineId ^(!MACHINE_ID!^) should
echo     appear within 30s of the agent starting.
echo.
endlocal

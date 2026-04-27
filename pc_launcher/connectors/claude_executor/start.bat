@echo off
REM start.bat -- launch the claude_executor agent against the NAS bridge.
REM Reads .env next to this .bat for bridge URL + token. Run install.bat
REM first to write the .env, then either run this manually or wire it to
REM Task Scheduler via register-task.bat (next to this file).

setlocal

REM .env lives next to this script.
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

if not exist "%SCRIPT_DIR%\.env" (
  echo [error] .env not found at %SCRIPT_DIR%\.env. Run install.bat first.
  exit /b 1
)

REM Load .env into the current shell. Skip blank lines and lines whose first
REM non-whitespace character is '#'. Note: cmd's `%%a` loop variables don't
REM support `:~0,1` substring, so we route through a per-line set first.
for /f "usebackq tokens=*" %%L in ("%SCRIPT_DIR%\.env") do (
  set "LINE=%%L"
  call :_apply_env
)
goto :env_loaded
:_apply_env
if "%LINE%"=="" exit /b 0
if "%LINE:~0,1%"=="#" exit /b 0
for /f "tokens=1* delims==" %%a in ("%LINE%") do set "%%a=%%b"
exit /b 0
:env_loaded

if not defined CLAUDE_BRIDGE_URL (
  echo [error] CLAUDE_BRIDGE_URL not set in .env.
  exit /b 1
)
if not defined CLAUDE_BRIDGE_TOKEN (
  echo [error] CLAUDE_BRIDGE_TOKEN not set in .env.
  exit /b 1
)

REM `python -m pc_launcher.connectors.claude_executor.runner` requires the
REM ops-cure repo root on sys.path. start.bat lives 3 dirs deep, so step
REM up to the repo root before invoking python.
set "REPO_ROOT=%SCRIPT_DIR%\..\..\.."
cd /d "%REPO_ROOT%"

echo Starting claude_executor against %CLAUDE_BRIDGE_URL%...
python -m pc_launcher.connectors.claude_executor.runner ^
  --bridge-url "%CLAUDE_BRIDGE_URL%" ^
  --token "%CLAUDE_BRIDGE_TOKEN%"
endlocal

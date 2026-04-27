@echo off
REM start.bat -- launch the remote_executor (codex) agent against the NAS bridge.
REM
REM Bridge URL comes from
REM   pc_launcher/projects/remote_executor/project.yaml (bridge.base_url)
REM Bridge token comes from
REM   pc_launcher/.env (BRIDGE_TOKEN)
REM
REM Both are read by the runner itself; this script just sets cwd and invokes
REM the module. Wire to Task Scheduler via register-task.bat (next to this file).

setlocal

REM `python -m pc_launcher.connectors.remote_executor.runner` requires the
REM ops-cure repo root on sys.path. start.bat lives 3 dirs deep, so step
REM up to the repo root before invoking python.
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "REPO_ROOT=%SCRIPT_DIR%\..\..\.."
cd /d "%REPO_ROOT%"

if not exist "pc_launcher\.env" (
  echo [warn] pc_launcher\.env not found. BRIDGE_TOKEN must be set in env or the runner will fail.
)
if not exist "pc_launcher\projects\remote_executor\project.yaml" (
  echo [error] project.yaml not found at pc_launcher\projects\remote_executor\project.yaml.
  exit /b 1
)

REM Mirror stdout + stderr to a rolling log so silent crashes (especially
REM under Task Scheduler, which discards inherited streams) leave a trail.
set "LOG_DIR=%REPO_ROOT%\..\_runtime\ops-cure\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1
set "LOG_FILE=%LOG_DIR%\remote_executor.log"

echo. >> "%LOG_FILE%"
echo === remote_executor start %DATE% %TIME% (pid %RANDOM%) === >> "%LOG_FILE%"

echo Starting remote_executor (codex) from %REPO_ROOT%... (logs -^> %LOG_FILE%)
python -u -m pc_launcher.connectors.remote_executor.runner >> "%LOG_FILE%" 2>&1
endlocal

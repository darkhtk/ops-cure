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

echo Starting remote_executor (codex) from %REPO_ROOT%...
python -m pc_launcher.connectors.remote_executor.runner
endlocal

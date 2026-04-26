@echo off
REM start.bat — launch the claude_executor agent against the NAS bridge.
REM Reads .env in the same directory (next to this .bat) for bridge URL +
REM token. Run install.bat first to write the .env, then either run this
REM manually or wire it to Task Scheduler for autostart.

setlocal
cd /d "%~dp0"

if not exist ".env" (
  echo [error] .env not found next to start.bat. Run install.bat first.
  exit /b 1
)

REM Load .env into the current shell (KEY=VALUE lines, ignore # comments).
for /f "usebackq tokens=1* delims==" %%a in (".env") do (
  if not "%%a"=="" if not "%%a:~0,1%"=="#" set "%%a=%%b"
)

if not defined CLAUDE_BRIDGE_URL (
  echo [error] CLAUDE_BRIDGE_URL not set in .env.
  exit /b 1
)
if not defined CLAUDE_BRIDGE_TOKEN (
  echo [error] CLAUDE_BRIDGE_TOKEN not set in .env.
  exit /b 1
)

set "REPO_ROOT=%~dp0..\..\..\.."
echo Starting claude_executor against %CLAUDE_BRIDGE_URL%...
python -m pc_launcher.connectors.claude_executor.runner ^
  --bridge-url "%CLAUDE_BRIDGE_URL%" ^
  --token "%CLAUDE_BRIDGE_TOKEN%"
endlocal

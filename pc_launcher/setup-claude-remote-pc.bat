@echo off
REM ============================================================================
REM  setup-claude-remote-pc.bat
REM
REM  One-shot enrollment of a new Windows PC as a claude-remote machine.
REM  Copy this file alone (USB / gist / signal) to the target PC and double-click.
REM
REM  What it does:
REM    1. Verifies prereqs (Python 3.11+, git, claude CLI optional, Tailscale optional)
REM    2. Clones / updates ops-cure under %USERPROFILE%\Projects\ops-cure
REM    3. pip installs requirements
REM    4. Prompts for bridge URL + token + machine id, writes the agent .env
REM    5. Registers ClaudeExecutor in Task Scheduler (logon trigger)
REM    6. Starts the agent once and tails the log for ~5 s
REM ============================================================================

setlocal EnableDelayedExpansion
echo.
echo === claude-remote PC enrollment ===
echo.

REM ---- 1. prereqs ------------------------------------------------------------

where python >nul 2>nul
if errorlevel 1 (
  echo [error] python not found on PATH. Install Python 3.11+ first:
  echo         https://www.python.org/downloads/  (check "Add to PATH")
  pause & exit /b 1
)
where git >nul 2>nul
if errorlevel 1 (
  echo [error] git not found on PATH. Install Git for Windows first:
  echo         https://git-scm.com/download/win
  pause & exit /b 1
)
where claude >nul 2>nul
if errorlevel 1 (
  echo [warn]  claude CLI not on PATH. Install with:
  echo           npm install -g @anthropic-ai/claude-code
  echo         The agent still starts but run.start commands will fail until claude exists.
  echo.
)
where tailscale >nul 2>nul
if errorlevel 1 (
  echo [warn]  tailscale not on PATH. Install + log in to the same tailnet so this
  echo         PC can reach the NAS bridge URL. (https://tailscale.com/download)
  echo.
)

REM ---- 2. clone / update repo -----------------------------------------------

set "DEFAULT_REPO=https://github.com/ymxclaude/ops-cure.git"
set /p REPO_URL="ops-cure repo URL [%DEFAULT_REPO%]: "
if "!REPO_URL!"=="" set "REPO_URL=%DEFAULT_REPO%"

set "DEFAULT_DIR=%USERPROFILE%\Projects\ops-cure"
set /p REPO_DIR="install dir [%DEFAULT_DIR%]: "
if "!REPO_DIR!"=="" set "REPO_DIR=%DEFAULT_DIR%"

if exist "%REPO_DIR%\.git" (
  echo Existing checkout found at %REPO_DIR% -- pulling latest.
  pushd "%REPO_DIR%" && git pull --ff-only && popd
  if errorlevel 1 (
    echo [warn] git pull failed -- continuing with whatever HEAD is checked out.
  )
) else (
  echo Cloning %REPO_URL% to %REPO_DIR% ...
  git clone "%REPO_URL%" "%REPO_DIR%"
  if errorlevel 1 (
    echo [error] git clone failed.
    pause & exit /b 1
  )
)

REM ---- 3. python deps -------------------------------------------------------

if exist "%REPO_DIR%\requirements.txt" (
  echo Installing Python dependencies (this can take a minute)...
  pushd "%REPO_DIR%"
  python -m pip install -r requirements.txt
  set "PIP_EXIT=!ERRORLEVEL!"
  popd
  if not "!PIP_EXIT!"=="0" (
    echo [error] pip install failed (exit !PIP_EXIT!).
    pause & exit /b 1
  )
) else (
  echo [warn] %REPO_DIR%\requirements.txt missing -- skipping pip install.
)

REM ---- 4. agent .env --------------------------------------------------------

set "AGENT_DIR=%REPO_DIR%\pc_launcher\connectors\claude_executor"
set "ENV_PATH=%AGENT_DIR%\.env"

if not exist "%AGENT_DIR%\install.bat" (
  echo [error] expected %AGENT_DIR%\install.bat -- repo layout changed?
  pause & exit /b 1
)

echo.
echo --- bridge config ---
echo.
echo Need the NAS bridge URL + a bearer token. The token must match BRIDGE_TOKEN
echo in the NAS's pc_launcher/.env (ask the admin or copy from a working PC's
echo agent .env).
echo.
set /p BRIDGE_URL="Bridge base URL (e.g. http://your-nas.example.ts.net:18080): "
if "!BRIDGE_URL!"=="" (
  echo [error] bridge URL is required.
  pause & exit /b 1
)
set /p BRIDGE_TOKEN="Bridge bearer token: "
if "!BRIDGE_TOKEN!"=="" (
  echo [error] bridge token is required.
  pause & exit /b 1
)
set "MACHINE_ID=%COMPUTERNAME%"
set /p USER_MACHINE_ID="Machine id [%MACHINE_ID%]: "
if not "%USER_MACHINE_ID%"=="" set "MACHINE_ID=%USER_MACHINE_ID%"

> "%ENV_PATH%" (
  echo # claude_executor -- written by setup-claude-remote-pc.bat
  echo CLAUDE_BRIDGE_URL=!BRIDGE_URL!
  echo CLAUDE_BRIDGE_TOKEN=!BRIDGE_TOKEN!
  echo CLAUDE_BRIDGE_MACHINE_ID=!MACHINE_ID!
)
echo Wrote %ENV_PATH%

REM ---- 5. register Task Scheduler entry -------------------------------------

echo.
echo --- registering Task Scheduler entry (ClaudeExecutor, logon trigger) ---
call "%AGENT_DIR%\register-task.bat"
if errorlevel 1 (
  echo [error] register-task.bat failed.
  pause & exit /b 1
)

REM ---- 6. start now + tail log briefly --------------------------------------

set "LOG_DIR=%REPO_DIR%\..\_runtime\ops-cure\logs"
set "LOG_FILE=%LOG_DIR%\claude_executor.log"
echo.
echo --- starting ClaudeExecutor now (independent of next logon) ---
schtasks /Run /TN "ClaudeExecutor" >nul
if errorlevel 1 (
  echo [warn] schtasks /Run failed; starting via PowerShell instead.
  powershell -NoProfile -Command "Start-ScheduledTask -TaskName 'ClaudeExecutor'"
)

echo Tailing %LOG_FILE% for 6 s ...
powershell -NoProfile -Command "$p='%LOG_FILE%'; $deadline=(Get-Date).AddSeconds(6); while ((Get-Date) -lt $deadline) { if (Test-Path $p) { Get-Content $p -Tail 10; break } else { Start-Sleep -Milliseconds 500 } }"

echo.
echo ============================================================================
echo  Done.
echo.
echo  Verify in the browser sidebar -- machine "!MACHINE_ID!" should appear within
echo  ~30 s. If not, inspect:
echo    %LOG_FILE%
echo.
echo  To stop the agent later:
echo    powershell -Command "Unregister-ScheduledTask -TaskName 'ClaudeExecutor' -Confirm:$false"
echo ============================================================================
endlocal
pause

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

REM ---- 1. prereqs (auto-install via winget) ---------------------------------

where winget >nul 2>nul
if errorlevel 1 (
  echo [error] winget not found. Install "App Installer" from Microsoft Store:
  echo         https://apps.microsoft.com/detail/9NBLGGH4NNS1
  echo         Then re-run this script.
  pause & exit /b 1
)

REM Common winget flags: silent + auto-accept license / source agreements.
REM A package already installed makes winget exit non-zero; we don't fail
REM the script on that since the post-install `where` check is the source
REM of truth.
set "WG_FLAGS=--silent --accept-source-agreements --accept-package-agreements -e --id"

call :_install_if_missing python  Python.Python.3.12
call :_install_if_missing git      Git.Git
call :_install_if_missing node     OpenJS.NodeJS.LTS
call :_install_if_missing tailscale tailscale.tailscale

REM Refresh PATH from registry so newly installed tools are visible to
REM THIS shell (not just future shells). Reads HKLM + HKCU Path values
REM and prepends both to %PATH%.
for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "_HKLM_PATH=%%~b"
for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "_HKCU_PATH=%%~b"
if defined _HKLM_PATH set "PATH=%_HKLM_PATH%;%PATH%"
if defined _HKCU_PATH set "PATH=%PATH%;%_HKCU_PATH%"

REM npm-installed CLIs land in %APPDATA%\npm by default; make sure that
REM directory is on PATH for the claude check below.
if exist "%APPDATA%\npm" set "PATH=%PATH%;%APPDATA%\npm"

REM claude CLI lives on npm, install it now that node should be on PATH.
where claude >nul 2>nul
if errorlevel 1 (
  where npm >nul 2>nul
  if errorlevel 1 (
    echo [warn]  npm still not on PATH after Node.js install. Open a NEW
    echo         shell and run:  npm install -g @anthropic-ai/claude-code
  ) else (
    echo Installing claude CLI via npm...
    call npm install -g @anthropic-ai/claude-code
  )
)

REM Final hard-required prereq check (python + git). claude / tailscale are
REM only warnings -- the agent boots without them but actual usage will fail.
where python >nul 2>nul
if errorlevel 1 (
  echo [error] python still not on PATH. Open a NEW shell and re-run this
  echo         script (PATH propagation can need a fresh process).
  pause & exit /b 1
)
where git >nul 2>nul
if errorlevel 1 (
  echo [error] git still not on PATH. Open a NEW shell and re-run.
  pause & exit /b 1
)
where claude >nul 2>nul
if errorlevel 1 echo [warn]  claude CLI missing -- run.start will fail until you install it.
where tailscale >nul 2>nul
if errorlevel 1 (
  echo [warn]  tailscale missing or not in PATH. The bridge URL will be
  echo         unreachable until Tailscale is installed AND you sign in
  echo         to the same tailnet. (winget did try to install it.)
)
echo.
goto :prereqs_done

:_install_if_missing
REM %1 = command to test, %2 = winget package id
where %1 >nul 2>nul
if errorlevel 1 (
  echo [winget] installing %2 ...
  winget install %WG_FLAGS% %2
) else (
  echo [winget] %1 already present, skip.
)
exit /b 0

:prereqs_done

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

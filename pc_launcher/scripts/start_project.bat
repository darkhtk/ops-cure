@echo off
setlocal

set SCRIPT_DIR=%~dp0
rem Suitable for Task Scheduler or Startup; launcher.py enforces a single-instance lock.
python "%SCRIPT_DIR%..\launcher.py" daemon --projects-dir "%SCRIPT_DIR%..\projects" %*


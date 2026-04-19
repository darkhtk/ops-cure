@echo off
setlocal

set SCRIPT_DIR=%~dp0
python "%SCRIPT_DIR%..\launcher.py" daemon --projects-dir "%SCRIPT_DIR%..\projects" %*


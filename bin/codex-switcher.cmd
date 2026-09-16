@echo off
rem codex（ChatGPT App）多平台模型切换 —— Windows 启动器
rem 用法：codex-switcher.cmd add --preset deepseek --key-stdin
setlocal

set "SCRIPT_DIR=%~dp0"
set "REPO_ROOT=%SCRIPT_DIR%.."

set "PY="
if exist "%REPO_ROOT%\.venv\Scripts\python.exe" set "PY=%REPO_ROOT%\.venv\Scripts\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\codex-switcher\python-path.txt" (
  set /p PY=<"%LOCALAPPDATA%\codex-switcher\python-path.txt"
)
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY set "PY=python"

set "PYTHONPATH=%REPO_ROOT%;%PYTHONPATH%"
%PY% -m codex_switcher %*
exit /b %ERRORLEVEL%

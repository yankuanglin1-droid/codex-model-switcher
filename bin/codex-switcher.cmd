@echo off
rem codex（ChatGPT App）多平台模型切换 —— Windows 启动器
rem 用法：codex-switcher.cmd add --preset deepseek --key-stdin
setlocal

rem 优先认安装目录（%~dp0..），其次认仓库根目录；都不写死绝对路径，
rem 免得中文用户名把路径写成乱码。
set "APPHOME=%~dp0.."
if not exist "%APPHOME%\codex_switcher" set "APPHOME=%~dp0..\.."

set "PY="
if exist "%APPHOME%\python-path.txt" (
  set /p PY=<"%APPHOME%\python-path.txt"
)
if not defined PY if exist "%APPHOME%\.venv\Scripts\python.exe" (
  set "PY=%APPHOME%\.venv\Scripts\python.exe"
)
if not defined PY if exist "%LOCALAPPDATA%\codex-switcher\python-path.txt" (
  set /p PY=<"%LOCALAPPDATA%\codex-switcher\python-path.txt"
)
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo 没有找到 Python。请到 https://www.python.org/downloads/windows/ 安装，
  echo 安装时记得勾选 Add python.exe to PATH。
  exit /b 1
)

set "PYTHONPATH=%APPHOME%;%PYTHONPATH%"
%PY% -m codex_switcher %*
exit /b %ERRORLEVEL%

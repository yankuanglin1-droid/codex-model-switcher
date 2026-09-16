# codex（ChatGPT App）多平台模型切换 —— Windows 安装脚本
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# 做四件事：找 Python、把运行时代码复制到 %LOCALAPPDATA%\codex-switcher、
# 生成 codex-switcher.cmd 启动器、把它加进用户 PATH。

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppHome  = if ($env:CODEX_SWITCHER_HOME) { $env:CODEX_SWITCHER_HOME }
            else { Join-Path $env:LOCALAPPDATA "codex-switcher" }
$BinDir   = Join-Path $AppHome "bin"

function Write-Step($text) { Write-Host $text }

function Find-Python {
    $candidates = @()
    foreach ($name in @("py", "python3", "python")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $candidates += $cmd.Source }
    }
    foreach ($exe in $candidates) {
        try {
            $probe = & $exe -c "import sys; print(1 if sys.version_info >= (3, 9) else 0)" 2>$null
            if ($probe -eq "1") { return $exe }
        } catch { }
    }
    return $null
}

$Python = Find-Python
if (-not $Python) {
    Write-Host "没有找到 Python 3.9 或更高版本。" -ForegroundColor Yellow
    Write-Host "请先安装：https://www.python.org/downloads/windows/  （安装时勾选 Add python.exe to PATH）"
    exit 1
}
$version = & $Python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
Write-Step "使用 Python：$Python（$version）"

if (Test-Path $AppHome) {
    Remove-Item -Recurse -Force (Join-Path $AppHome "codex_switcher") -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force (Join-Path $AppHome "packaging") -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Force -Path $AppHome | Out-Null
New-Item -ItemType Directory -Force -Path $BinDir  | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $AppHome "packaging\macos") | Out-Null

Copy-Item -Recurse -Force (Join-Path $RepoRoot "codex_switcher") $AppHome
Copy-Item -Force (Join-Path $RepoRoot "packaging\macos\launch.sh") (Join-Path $AppHome "packaging\macos\launch.sh")
Write-Step "已安装运行时：$AppHome"

# 启动器：.cmd 给命令行用，.vbs 给「双击不弹黑框」用
#
# 编码这里很重要：中文 Windows 的用户名（C:\Users\张三\…）会让路径带中文，
# 用 -Encoding ASCII 写出来会变成一堆问号，启动器就永远找不到 Python 了。
# 所以统统一律写 ANSI（系统本地代码页），cmd.exe 和 VBScript 正好都按这个读。
$CmdPath = Join-Path $BinDir "codex-switcher.cmd"
@"
@echo off
setlocal
rem 用相对路径定位安装目录，避免把可能带中文的绝对路径硬写进来
set "APPHOME=%~dp0.."
set "PY="
if exist "%APPHOME%\python-path.txt" set /p PY=<"%APPHOME%\python-path.txt"
if not defined PY if exist "%APPHOME%\.venv\Scripts\python.exe" set "PY=%APPHOME%\.venv\Scripts\python.exe"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY set "PY=python"
set "PYTHONPATH=%APPHOME%;%PYTHONPATH%"
%PY% -m codex_switcher %*
exit /b %ERRORLEVEL%
"@ | Set-Content -Encoding Default $CmdPath

$VbsPath = Join-Path $BinDir "codex-switcher-gui.vbs"
@"
Set shell = CreateObject("WScript.Shell")
shell.Run "cmd /c """"$CmdPath"""" app", 0, False
"@ | Set-Content -Encoding Default $VbsPath

Set-Content -Encoding Default (Join-Path $AppHome "python-path.txt") $Python
Write-Step "已生成启动器：$CmdPath"
Write-Step "图形界面快捷方式：$VbsPath（双击不会弹黑框）"

# 桌面快捷方式：不然用户根本找不到 %LOCALAPPDATA% 下面那个 .vbs
try {
    $desktop = [Environment]::GetFolderPath("Desktop")
    if ($desktop) {
        $lnkPath = Join-Path $desktop "Codex 多平台模型切换.lnk"
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($lnkPath)
        $shortcut.TargetPath = "$env:SystemRoot\System32\wscript.exe"
        $shortcut.Arguments = '"' + $VbsPath + '"'
        $shortcut.WorkingDirectory = $AppHome
        $shortcut.Description = "Codex（ChatGPT App）多平台模型切换"
        $shortcut.Save()
        Write-Step "已创建桌面快捷方式：$lnkPath"
    }
} catch {
    Write-Step "（桌面快捷方式没建成，可以直接双击：$VbsPath）"
}

# 加进用户 PATH（只影响当前用户）
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$BinDir*") {
    $newPath = if ([string]::IsNullOrEmpty($userPath)) { $BinDir } else { "$BinDir;$userPath" }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Step "已把 $BinDir 加进用户 PATH（新开一个终端生效）"
}

Write-Step ""
Write-Step "初始化……"
$env:PYTHONPATH = $AppHome
& $Python -m codex_switcher init

Write-Step ""
Write-Step "完成。接下来："
Write-Step "  1) 新开一个 PowerShell / CMD，运行："
Write-Step "       codex-switcher add --preset deepseek --key-stdin"
Write-Step "  2) 切换：codex-switcher use deepseek"
Write-Step "  3) 图形界面：双击 $VbsPath"
Write-Step "  4) 完全退出并重新打开 ChatGPT App / Codex"

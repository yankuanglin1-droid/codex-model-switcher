$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$Stage = Join-Path $Root 'dist/ChatGPT-Model-Switcher-Windows-x64'
New-Item -ItemType Directory -Force $Stage | Out-Null
$Runtime = Join-Path $Stage 'python'
$Download = Join-Path $env:TEMP 'switcher-python-embed.zip'
Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip' -OutFile $Download
Expand-Archive $Download $Runtime -Force
@('python312.zip', '.', '..', 'Lib/site-packages', 'import site') | Set-Content -Encoding ASCII (Join-Path $Runtime 'python312._pth')
python -m pip install --disable-pip-version-check --target (Join-Path $Runtime 'Lib/site-packages') 'pywebview==6.1'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
Copy-Item -Recurse -Force (Join-Path $Root 'codex_switcher') $Stage
New-Item -ItemType Directory -Force (Join-Path $Stage 'packaging/windows') | Out-Null
Copy-Item (Join-Path $PSScriptRoot 'app.py'),(Join-Path $PSScriptRoot 'loading.html') (Join-Path $Stage 'packaging/windows')
Copy-Item (Join-Path $Root 'LICENSE') $Stage
@'
@echo off
cd /d "%~dp0"
start "" "%~dp0python\pythonw.exe" "%~dp0packaging\windows\app.py"
'@ | Set-Content -Encoding ASCII (Join-Path $Stage 'Start.cmd')
@'
@echo off
"%~dp0python\python.exe" -m codex_switcher %*
'@ | Set-Content -Encoding ASCII (Join-Path $Stage 'codex-switcher.cmd')
Copy-Item (Join-Path $PSScriptRoot 'README.txt') $Stage
& (Join-Path $Runtime 'python.exe') (Join-Path $Stage 'packaging/windows/app.py') --self-test
if ($LASTEXITCODE -ne 0) { throw 'Embedded runtime smoke test failed' }
Get-ChildItem $Stage -Directory -Recurse -Filter '__pycache__' | Remove-Item -Recurse -Force
Compress-Archive (Join-Path $Stage '*') (Join-Path $Root 'dist/Codex-Model-Switcher-Windows-x64-portable.zip') -Force

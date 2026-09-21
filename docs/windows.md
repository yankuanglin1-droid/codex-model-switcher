# Windows edition

Two entry paths are provided:

- Source installer: run `install.ps1` with Python 3.9+ already installed. The
  existing desktop shortcut opens the browser-based UI.
- Native portable build: on Windows x64 with Python 3.12, run
  `powershell -File packaging/windows/build.ps1`. This downloads the official
  Python embedded runtime and packages pywebview plus local UI assets. The ZIP
  includes Python; users extract to a permanent folder and launch `Start.cmd`.
  Requires Microsoft Edge WebView2 Runtime. The packaged Python is also used for
  credential helpers, avoiding frozen-executable interpreter issues.

The build includes a runtime import/static-asset smoke test. It does not claim a
successful user-visible window or real provider execution until tested on Windows.
The Windows CI template is at `windows-ci.example.yml`. It must be installed as
`.github/workflows/windows.yml` by an authorized GitHub identity; current OAuth
permissions rejected that operation. No hosted Windows build has run yet.

Windows does not currently implement the macOS graceful host-restart/history
repair action. Source compatibility is not equivalent to desktop feature parity.

Official implementation references:
- https://pywebview.flowrl.com/guide/web_engine
- https://www.python.org/downloads/windows/

## 中文

已有源码安装方式和 Windows x64 独立窗口便携版的构建脚本。源码安装需自行
安装 Python；便携版构建后自带 Python，用户完整解压后双击 Start.cmd 即可。
依赖微软 WebView2 Runtime。凭据使用当前用户的本地 DPAPI 保护。

本次修复了含空格的 Python 路径启动失败，以及仅凭 python.exe 名称判断进程
身份的问题。无法核实身份时不会终止进程。

目前 GitHub OAuth 授权缺少 workflow 权限，Windows 自动构建尚未运行；本版
提供源码包，不能把它称为已验证的 Windows 成品安装包。macOS 自动退出宿主
并修复历史的功能尚未移植到 Windows，界面会明确报告不支持。

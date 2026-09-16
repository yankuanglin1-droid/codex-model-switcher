# 参与开发

## 先跑起来

```bash
git clone https://github.com/yankuanglin1-droid/codex-model-switcher.git
cd codex-model-switcher
bash install.sh                     # macOS / Linux
# powershell -ExecutionPolicy Bypass -File install.ps1   # Windows
codex-switcher doctor
```

只用标准库，**没有 pip 依赖**，所以不需要建虚拟环境。

## 改完必须做的事

```bash
python3 tools/preflight.py
```

一条命令跑完 9 组检查。**每一组都对应一个真实发生过的错误**，不是凭空加的规则：

| 检查 | 当初踩的坑 |
| --- | --- |
| 密钥 / 个人路径（含 **git 历史**） | 仓库里混进 Key；删掉了却还留在提交历史里 |
| 平台专属写法 | Windows 上 `import fcntl` 直接崩，启动都起不来 |
| 旧名残留 | 同一个东西三套名字，其中一个是错的 |
| 文档相对链接 | 写了指向不存在文件的链接 |
| App 名三处一致 | README 让人找的 `.app` 名字和实际不符，照着做找不到文件 |
| Python 版本门槛（**行为验证**） | 记下的解释器卡 3.11，把系统自带的 3.9 排除了 |
| Windows 启动器编码 | 用 ASCII 写启动器，中文用户名路径全变问号 |
| shell 语法 | 5 个脚本 |
| 测试套件 | 功能回归 |

单跑某一项也行：

```bash
python3 -m unittest discover -s tests      # 71 项
python3 tools/check_portability.py         # 平台专属写法
python3 tools/scan_secrets.py --history    # 密钥（含历史）
```

## 几条硬性约束

- **密钥绝不落盘到仓库相关的地方。** 只进系统密钥库（macOS 钥匙串 / Windows DPAPI /
  Linux Secret Service）。命令行参数里的 Key 会进 shell 历史，一律用 `--key-stdin`。
- **不要手写 `wire_api`。** 现在必须是 `responses`；写 `chat` 会让 Codex 直接拒绝启动。
  只讲 Chat Completions 的平台走本地协议桥翻译。
- **provider id 不能用保留名**：`openai`、`ollama`、`lmstudio`、`codex`、`azure`。
  撞了会导致 Codex 忽略整个模型列表。
- **只改模型相关字段。** `config.toml` 里其余内容必须逐字节不变，改前先备份、改后复验。
- **拿不到的数据就说拿不到。** 余额/额度查不到时显示「未开放接口」并给官网入口，
  不要编数字。

## 发版

```bash
# 1) 改版本号：codex_switcher/__init__.py 里的 __version__
# 2) 自检
python3 tools/preflight.py
# 3) 打 App 与安装包（完整版 + 标准版；会自动生成固定名副本）
bash packaging/macos/build_app.sh --with-python
bash packaging/macos/build_app.sh
bash packaging/macos/package_release.sh --full --app "/tmp/build/codex（ChatGPT App）多平台模型切换.app"
bash packaging/macos/package_release.sh
# 4) 建 Release，四个附件都传（含两个 -latest 固定名副本）
```

`-latest` 那份是为了让 README 里的下载链接永久有效——
`releases/latest/download/<固定名>` 只会去最新那个 Release 里找同名附件。

## 界面截图与演示图

```bash
python3 tools/make_screenshot.py --gif
```

在一个临时 `CODEX_HOME` 里造演示平台，用无头 Chrome 截图并合成 `docs/demo.gif`。
**不会写真实的钥匙串**：脚本强制把凭据后端切成文件回退，且只写假密钥。

## 许可

提交即表示同意以 MIT 许可发布你的贡献。

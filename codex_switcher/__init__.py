"""Codex Model Switcher —— 把任意 OpenAI 兼容平台接进 Codex 并随时切换。

设计原则：
1. API Key 只进系统密钥库（macOS 钥匙串 / Windows DPAPI / Linux Secret Service），
   永不写入 config.toml。
2. 改配置前先备份，改完用 TOML 解析器复验，除模型相关字段外必须逐字节不变。
3. 拿不到的数据就如实说“拿不到”，绝不编造余额或额度。
"""

__version__ = "1.6.6"
PROJECT_URL = "https://github.com/yankuanglin1-droid/codex-model-switcher"

__all__ = ["__version__", "PROJECT_URL"]

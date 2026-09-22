# History preservation and recovery / 历史保护与恢复

## v1.7.11 safety changes

The switcher preserves existing transcripts during startup, default-model changes,
background diagnostics and ordinary restart. Existing tasks and automations keep
their provider bindings. A new default applies to new tasks.

Older versions could rewrite rollout files without updating the host's paginated
history indexes. A saved byte offset can then point beyond EOF or into a JSON
record: the original file may still contain messages that the UI cannot read.

## 恢复现有任务历史

macOS App 的历史检查在线运行且只读：它不会退出 ChatGPT/Codex，也不会
改写数据库、分页游标或原始任务记录。宿主运行时，切换器只报告可读性与
索引风险，避免把缓存中的会话再次损坏。

如需进行离线重建，必须由维护者在宿主**已经停止**时单独运行受控恢复流程；
App 按钮不会主动关闭或重开 ChatGPT/Codex。该流程仅用于已确认的分页索引
故障，默认不会重写 `msg`、`fc`、`call_id` 或 `reasoning.content`：

1. 确认宿主已停止，并确认旧后台进程与外部数据库句柄已退出；退出失败或中途重新打开会停止写入。
2. 仅选择当前未归档任务。归档任务、仅备份中存在的任务、无法确认归属的文件不纳入恢复。
3. 数据库快照、所有纳入恢复的原文件独立备份和逐任务报告存于本机 `CODEX_HOME/model-switcher-preservation/`，不进入滚动清理、安装包或 GitHub。
4. 在隔离副本中调用已安装的官方历史读取程序重建分页记录。副本不含登录凭据，不启动模型推理、不执行工具，也不把真实历史路径传给 `thread/resume`。
5. 只补充缺失的显示记录，核对任务归属和源文件校验值；已有消息内容发生冲突时保留原值并报告。派生的轮次读取位置按已验证的官方重建结果同步。
6. 通过官方分页读取接口验证数量和显示内容校验值。部分失败明确显示为待检查，不宣布全部恢复成功。

恢复报告区分「已备份」「已处理」「已读回验证」，不会把备份数量作为
恢复成功数量。失败时另存本机 `failure.json`，记录具体步骤、固定错误代码
和代码位置，不记录聊天正文或服务端错误正文。原始检查报告仍保留。

每次原文件改写或显示索引导入都有独立撤销凭据。验证失败时先确认本次
读取器进程已经退出，再核对文件及数据库行仍为本次操作的结果，才允许
定向回滚。发现新消息、外部占用或未完成清理时保留现状并标记待处理；
不会用旧备份覆盖新工作，也不会在回滚尚未确认时自动重开宿主。

**不得直接删除分页游标或整个历史数据库。** 已知宿主 schema 的 DELETE trigger 会连带清理实时记录；本工具只将游标的读取位置归零，保留 cursor 和消息数据。未知 schema、未知触发器、数据库锁定或并发变化均停止相关写入。

## 协议兼容

- 不会为跨平台切换修改 `msg` / `fc` / `rs`、`call_id` 或 `reasoning.content`。这些字段是服务商协议状态，必须跟随原任务保留。
- 旧任务需要改用另一服务商时，创建兼容续接任务并传递可移植的文本、附件引用和任务状态；不能把另一服务商的隐藏推理或工具协议状态伪装成目标服务商格式。
- Chat Completions 协议桥只转换请求副本。缺失、重复或孤儿 `call_id`，以及无法表达的工具/内容类型，会返回明确错误，不再伪造 ID 或静默丢弃记录。
- 普通重启不修复历史。旧的 `sanitize` / clean 路径已改为诊断，不再删除记录。

## Verification and limits

Regression tests cover archive exclusion, active-host refusal, byte/inode races,
transaction rollback, cleanup triggers, replay isolation, idempotent imports,
streamed tool identities, readback hashes and read-only default switching.
Run them in a temporary `CODEX_HOME`; tests use synthetic providers and histories.

Before a release, use the installed host to replay private copies and verify all
turn offsets against the original source, then check display hashes across
restarts. A stable count alone does not prove complete history. Offline replay
does not prove successful inference against every provider.

Automatic host shutdown and recovery are currently implemented on macOS. Windows
configuration and protocol logic have portable tests; Windows recovery stays
read-only until equivalent host-shutdown and replay checks are available.

No tool can reconstruct records with no surviving file or backup, or guarantee
that every future host/API version remains compatible. Conflicting, malformed or
unverifiable data is preserved for review instead of silently rewritten.

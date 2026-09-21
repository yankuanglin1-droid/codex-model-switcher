# History preservation / 原始对话保护（v1.7.10）

修复目标是防止模型切换器导致重启后历史显示减少。旧版默认迁移和自动清扫可能直接删除历史中的工具、图片或推理条目；文件占用检测失败时退回修改时间判断也不够安全。

## 现在的行为

- 打开 App、切换默认模型、后台检查不再写入已有任务历史或任务数据库。
- 旧任务与自动化保留原平台。新任务使用新的默认平台。不要在旧任务中直接选择另一平台的模型；这不构成安全迁移。
- 显式迁移只修改平台字段及必要的官方记录 ID，保留所有历史记录、工具结果、图片和推理。不能保证每种专有工具或加密历史被其他平台接受。
- 显式修复遇到忙碌、无法检测占用、文件缺失或读取失败时整条延期，不单独修改数据库，也不报告已成功。
- 每次写入前重新检查文件句柄、设备、inode、mtime、大小与字节内容。失败保留原文件，成功后读回验证。
- 官方历史 ID 修复仍通过“修复官方历史并重启”入口执行，先退出宿主并备份；不会删除对话正文或工具结果。

后台不再执行破坏性转换。低层 CLI 中显式清扫功能仍属于手动维护操作，不应当作恢复缺失消息的方法；请先保存备份。旧版文档中的自动批量迁移/清扫说明不再适用。

## 使用上的边界

这是对切换器已知写入路径的修复，并非所有平台、宿主版本或文件系统永远不会出错的承诺。它防止后续自动删改，不会凭空恢复已经删除、尚未落盘或宿主无法显示的消息。恢复旧任务需要逐任务比较历史、备份和显示结果，避免覆盖后来新增内容。

Windows 上无法确认文件占用时，手动写入会被阻止；不会退回“mtime 足够老就能写”的策略。Windows 本轮未作实机验收。

## Verification

235 tests passed on macOS. New regression fixtures cover unknown occupancy, lsof error output, late changes before writes, replacement inode with identical content, immutable history/database during default switches, read-only startup/watchdog, and database deferral when history cannot be updated. Tests use temporary histories and synthetic providers, never private sessions or credentials.

Startup and switching are read-only for existing transcripts. Explicit migration preserves every source record. This does not provide universal cross-provider replay compatibility or guarantee restoration of UI messages lost before this update.

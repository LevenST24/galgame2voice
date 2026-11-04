# 中断回复身份关联与单句语音修复

## 1. 背景与核心问题

流式回复在收到部分台词后中断（网络断开或用户停止）时，服务端仅持久化部分台词（partial assistant），前端在未收到 `done` 事件前无法获知该回复在数据库中的实际 `message_id`。此时若在 LOG 中单句重新生成语音，因缺少可信的服务端消息编号，修复音频只能保存在浏览器内存与本地存储；后续重新加载历史分页时，服务端旧记录会覆盖仅浏览器存在的修复音频。

本机制在用户行提交后、首句台词之前建立用户消息关联，使中断回复在修复前能通过只读接口精确解析出助手行 ID 并持久化写回服务端，杜绝猜测 `m_` 编号或使用临时 ID。

## 2. 流式协议与身份边界

1. **`turn_started` 事件**：
   - 触发时机：用户行提交后、首句台词（`line` 事件）下发前（注：排队 `queued` 或模型思考 `thinking` 等 `progress` 事件可能更早发出，避免绝对首个 SSE 声明）。
   - 载荷格式：`{ "session_id": "...", "user_message_id": 123 }`。
   - 身份约束：`user_message_id` 为当前轮次用户行 ID，前端存储为 `serverUserMessageId`，**绝非助手消息 ID**，严禁直接作为回复 ID 使用。
   - 冲突处理：同一轮若收到不同用户消息 ID，前端标记 `turnConflict: true` 拒绝认领，后续禁止发起自动关联。
2. **正常完成零开销**：正常结束的流式由 `done` 事件下发最终持久化后的 `message_id`，正常路径不产生额外 GET 查询。

## 3. 只读解析接口 (`GET /api/chat/turn-reply`)

- **请求参数**：`session_id` 与 `user_message_id`。
- **快照与隔离**：单 SQL 一致只读快照（`messages u LEFT JOIN messages a`），不获取 turn 会话写锁，采用独立 `timeout=0` 连接避免等待默认 5 秒 busy 锁。
- **回显与五状态语义**：响应必须严格回显 `session_id` 与 `user_message_id`：
  - `ready`：成功匹配到唯一助手消息，返回正整数 `message_id`；
  - `pending`：用户行已存在但助手回复尚未提交入库，允许退避重试；
  - `gone`：指定会话或对应用户消息已删除/清空，立即终止；
  - `ambiguous`：存在多条匹配回复，严禁任选或猜 ID，立即报错；
  - `unavailable`：SQLite 锁忙或只读超时，允许退避重试。

## 4. 前端调度与所有权防护

1. **按需有界解析**：
   - 仅在已有可信 `serverUserMessageId` 且缺少 `serverMessageId` 时按需发起解析。
   - 总预算 10 秒、最多 3 次，单次请求超时 2.5 秒，间隔退避 300ms。
2. **生命周期与所有权核验**：
   - 每次网络请求前后与状态写回前，通过 `checkCurrent()` 严格比对会话实例、消息节点、台词文本、音色绑定与 turn 键；会话切换、删除或取消均触发 `AbortError`，禁止迟到响应写回。
   - 校验服务端回显键值完整匹配后，方将 `message_id` 写入 `serverMessageId` 并发送修复 POST。
3. **退避与兜底**：
   - `pending` / `unavailable` 超时或遭遇 `gone` / `ambiguous` 时给出中文指引，保留本地现有音频，绝不向服务端发送 null ID。
   - 无任何服务端 ID 的历史导入记录保留原 browser-only 本地修复能力。

## 5. 数据库模式与备份隔离

- **增量补列**：`messages.reply_to_message_id` 采用 `_add_column_if_missing` 增量添加可空 INTEGER 列，`CURRENT_SCHEMA_VERSION = 7` 保持不变，建立普通查询索引。
- **数据兼容与备份隔离**：旧数据保持 NULL 不猜测回填；备份导出与导入严格剥离该字段，禁止将旧库 ID 映射污染至新库。

## 6. 验证与交付边界

- **端到端集成验证**：通过真实本机 HTTP / SSE / SQLite 全链路测试（1 项独立复跑通过，接收 feature 后再跑 1 pass 通过）。使用 FakeDOM 驱动真实前端 `initApp`，完整打通“用户提问提交 → 收到部分台词后中断 → 状态先 pending 后 ready → 携带唯一非空助手 ID 发起修复 POST → SQLite 更新 `lines_json` → 按 `history?limit=100` 重新初始化拉取历史 → 向同一音频 URL 发起真实 GET 200 返回 RIFF/WAVE”。数据库旧 202 条核心列逐行未改，总行数由 202 增至 204；生命周期 `finally` 覆盖由静态确认，正常路径清理执行通过。
- **历史回归基础（开发期）**：历史定点测试中，638 项相关后端测试、前端完整 `npm test` 57 脚本串行链及 `npm run deploy` 均 exit 0 通过。该阶段采用的替身为 FakeDOM、fake player、fake voice manager/profile switch、fake memory、非核心 config/assets/devices/version 桩接口及假 LLM/TTS 生成。
- **最新 v3.1.1 节点验收证据**：在 v3.1.1 本地发布候选节点中，已通过完整 3720 项后端测试、全量前端测试以及真实 Windows 便携版 EXE + Chromium 浏览器端到端验收（覆盖单轮对话、中断恢复后 LOG 单句语音 HTTP 取回、备份导出与重启后 SQLite `reply_to_message_id` 持久化关联）。
- **未验证项**：当前测试均基于本地替身与合成上游；真实物理 GPU 显存负载、公网商业模型 API 与人工听感品质仍未验证。详细交接说明见 [RELEASE_HANDOFF_3_1_1.md](RELEASE_HANDOFF_3_1_1.md)。

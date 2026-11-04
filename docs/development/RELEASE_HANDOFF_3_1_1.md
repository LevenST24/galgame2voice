# Galgame2Voice v3.1.1 发布交接文档 (Release Handoff)

本文档为 Galgame2Voice v3.1.1 发布节点（自 `origin/main` `ee56fa0` 起的所有已验收工作）提供仓库原生、可执行的交接说明。

> [!IMPORTANT]
> **维护者合并策略 (Maintainer Merge Policy)**
> - **CI 门禁要求**：合入 `main` 前，必须确保当前 HEAD 在 GitHub Actions 上的全部 6 项 CI 检查均执行成功：
>   1. `lint-and-test (3.10)`
>   2. `lint-and-test (3.12)`
>   3. `test-linux`
>   4. `dependency-audit`
>   5. `frontend-build`
>   6. `docker-build`
> - **审查与元数据记录**：实际的代码与打包审计步骤必须通过；CI 运行结果与状态记录在对应 PR 的 Checks 选项卡中，维护者在检查通过后将规范链接更新至 PR 描述（PR Description）元数据中（不向已提交的代码仓库追加提交，避免再次触发 CI 循环）。

---

## 1. 范围与交付概览

- **代码范围**：自基线提交 `ee56fa0` 以来的全部已验收特性与修复（包含累计 16 项独立缺陷修复与 1 项冻结回复持久化工程门禁）。
- **发布产物**：Windows x64 便携版发布包（本地验证大小：58,447,742 字节，约 58.45 MB），包含完整 Python 运行时与 FFmpeg，不包含个人角色模型、私有数据或真实凭据。
- **静态资源**：前端入口经 Vite 构建生成并同步部署在 `galgame2voice/static/`，当前产物入口文件为 `index-BUiKa_Vm.js`，样式为 `index-Skqi1XQu.css`。

---

## 2. 核心架构意图与模块入口

### 2.1 大模型流式完成策略 (Completion Policy)
- **核心模块**：
  - `galgame2voice/adapters/base.py` (`validate_stream_completion`)
  - `galgame2voice/adapters/llm/openai_adapter.py`
  - `galgame2voice/adapters/llm/anthropic_adapter.py`
  - `galgame2voice/utils/error_diagnostics.py` (`StreamTruncatedError`, `StreamIncompleteError`)
- **设计契约**：
  - 严格区分正常自然结束、长度截断（`max_tokens`/`length`）以及网络意外断流（premature EOF）。
  - 当流式响应因长度超限或意外中断提前结束时，统一抛出明确的诊断异常，保留已输出的部分文本在 LOG 中供用户查看与重试，同时**不结算好感度**，防止不完整的截断回复误增好感。

### 2.2 参考音频归属与音色回退 (Reference Origin & Voice Resolver)
- **核心模块**：
  - `galgame2voice/services/reference_origin.py` (`ReferenceOrigin`, `classify_reference_origin`)
  - `galgame2voice/services/voice_resolver.py`
  - `galgame2voice/services/tts_service.py`
  - `galgame2voice/services/character_manager.py`
  - `galgame2voice/services/audio_identity.py`
  - `galgame2voice/services/gpt_sovits_client.py`
- **设计契约**：
  - 引入 `ReferenceOrigin` 分类器（将参考音频清晰分类为：`EMPTY`、`CROSS_PACKAGE`、`PACKAGE_OWNED`、`EXTERNAL_OR_CUSTOM`）。
  - 音色解析或回退时，必须将参考音频路径、提示文本（prompt_text）与提示语言（prompt_lang）作为一个不可分割的整体联动更新，杜绝跨角色包借用同名音频。
  - 权重缓存与请求载荷构建基于解析后的音色身份隔离，防止引擎缓存混用上一个角色的模型或语言配置。

### 2.3 中断回复身份与单句语音持久化 (Turn Reply Identity & Line Audio Recovery)
- **核心模块**：
  - `galgame2voice/routers/chat.py` (`GET /api/chat/turn-reply`)
  - `galgame2voice/database/crud_modules/sessions.py` (`lookup_turn_reply`)
  - `frontend/src/controllers/line_audio_recovery.js`
  - `frontend/src/conversation.js`
- **设计契约**：
  - 用户消息入库后，流式首句台词前发送携带 `user_message_id` 的 `turn_started` SSE 事件。
  - 当流式中断后在 LOG 中重新生成单句语音时，前端通过 `/api/chat/turn-reply` 只读接口按已确认的用户消息 ID 精确解析服务端生成的助手 `message_id`，杜绝猜测临时编号。
  - 重新合成成功的单句音频 URL 写回 SQLite 数据库，刷新页面或重新分页拉取历史记录时音频持久有效。详细规格见 [TURN_REPLY_IDENTITY.md](TURN_REPLY_IDENTITY.md)。

### 2.4 源码更新持久状态与恢复 (Update State & Durable Recovery)
- **核心模块**：
  - `galgame2voice/services/update_state.py` (`PENDING_UPDATE_FILE = ".update_pending.json"`, `save_update_pending_state`, `load_update_pending_state`, `clear_update_pending_state`)
  - `galgame2voice/routers/system.py` (`_execute_update_pull_and_build`)
  - `frontend/src/controllers/update_manager.js`
  - `frontend/src/settings.js`
- **设计契约**：
  - 源码版更新前，若备份失败立即中止并保留未知的自定义静态资源，防止误删用户文件。
  - 若源码版拉取后 `npm run deploy` 编译前端静态产物因环境异常退出，更新系统将 `build_pending` 待办状态持久化落盘至 `data/.update_pending.json`；服务重启后状态依然保留，界面醒目提示并提供「重试前端构建并更新」入口，成功部署后自动清除标记（便携版不适用在线拉取更新，通过发布包整体替换）。

### 2.5 设置中心测试所有权与取消控制 (Settings Test Ownership)
- **核心模块**：
  - `frontend/src/controllers/global_settings.js` (`invalidateActiveProviderTest`, `isProviderTestCurrent`, `providerTestRevision`)
  - `frontend/src/api.js`
- **设计契约**：
  - 模型连通性测试引入 `AbortController`、代数计数器（`providerTestRevision`）与输入草稿快照。
  - 当用户在测试进行中切换提供商、修改 URL/模型/密钥草稿或关闭设置弹窗时，立即中止旧请求，重置测试按钮状态；迟到返回的成功或失败响应自动作废，绝不覆盖新界面的状态与草稿。

### 2.6 Telegram 重置协调器与生命周期 (Telegram Reset Coordinator)
- **核心模块**：
  - `galgame2voice/telegram_bot/handlers.py`
  - `galgame2voice/telegram_bot/console_menus.py`
  - `galgame2voice/telegram_bot/bot.py`
- **设计契约**：
  - 收到 `/reset` 指令或控制台清空事件时，统一作废并取消当前成员在语音下载、转码、STT 识别、LLM 生成及排队中的所有在途任务。
  - 重置响应发出后，旧回复正文与旧错误通知全部丢弃，杜绝孤儿记录入库、错误好感度增加及后台多余语音合成任务回流。群组多成员并发时按会话及成员严格隔离。

---

## 3. 数据库模式兼容与凭据安全

### 3.1 数据库架构兼容性
- **版本号**：本次变更保持 `CURRENT_SCHEMA_VERSION = 7` 不变。
- **增量字段**：在 `galgame2voice/database/migrations.py` 中通过 `_add_column_if_missing` 增量添加可空列 `messages.reply_to_message_id INTEGER DEFAULT NULL`，并建立普通索引 `idx_messages_session_reply_to`。
- **历史数据与备份隔离**：历史已存记录保持 `NULL`，不进行猜测性回填；聊天备份导出与导入严格排除数据库内部的 `reply_to_message_id`，防止旧库或异机 local ID 污染新库。详见 [TURN_REPLY_IDENTITY.md](TURN_REPLY_IDENTITY.md)。

### 3.2 凭据加密与迁移边界 (按 SECURITY.md 规范)
- **写入加密**：`galgame2voice/security/crypto.py` 导入 `cryptography` 的 `AESGCM`。Windows 环境写入 DPAPI 加密凭据（`dpapi:` 前缀）；非 Windows 环境（或 Windows DPAPI 保护失败回退时）使用 AES-GCM 加密（`enc:` 前缀）。
- **解密与错误处理**：解密时遇到缺失或无效的主密钥绝不降级为明文，而是明确报错中断（fail-closed，抛出 `MasterKeyError` 或返回空字符串）。
- **Linux/Docker 主密钥边界**：Linux 下 AES 主密钥保存在 `data/.master_key`（或由 `GALGAME2VOICE_SECRET_KEY`/`GALGAME2VOICE_MASTER_KEY` 注入）。若密钥文件与数据库存放在同一数据卷中，仅防范直接查看数据库文件的明文泄露，不防御整个数据卷失窃；主密钥损坏或无读权限时严格拒绝启动或解密。
- **历史明文兼容**：历史明文凭据仅在读取与启动迁移时向后兼容解码（`upgrade_secret_encryption`），新写入一律强制加密。
- **跨机/跨账户迁移**：Windows DPAPI 凭据严格绑定当前 Windows 用户账户上下文；跨机器或跨 Windows 用户迁移便携包时，DPAPI 凭据无法直接解密，需在界面中重新输入保存。
- **路径便携性**：项目根目录下的音色路径在数据库中规范化为项目相对路径存储，项目整体移动时保持相对路径有效；项目外部路径（如其他盘符的外部素材或外部 GPT-SoVITS 目录）若发生物理移动仍需重新选择。

---

## 4. 标准门禁复现命令

在标准开发机环境中，从**仓库根目录**执行以下命令验证全部门禁（使用 `npm --prefix frontend` 确保工作目录停留在仓库根目录）：

### 4.1 前端测试与构建
```powershell
# 1. 运行完整前端单元与集成测试
npm --prefix frontend test

# 2. 构建前端静态产物并验证与静态托管目录同步
npm --prefix frontend run deploy
```

### 4.2 后端代码规范与全量测试
```powershell
# 1. 执行代码规范检查
uv run ruff check galgame2voice scripts

# 2. 执行完整后端自动化测试（排除真实角色素材标记）
uv run pytest -q -m "not requires_character_assets"
```

### 4.3 Windows 便携包构建与真实 Smoke 验收
```powershell
# 1. 打包 Windows 便携版
uv run --frozen --extra desktop python scripts/build_windows_portable.py

# 2. 校验便携包内容、依赖与静态产物一致性
uv run --frozen --extra desktop python scripts/verify_windows_portable.py dist/galgame2voice-v3.1.1-windows-x64.zip

# 3. 基础启动与恢复 Smoke 检查
uv run --frozen --extra desktop python scripts/smoke_windows_portable.py dist/galgame2voice-v3.1.1-windows-x64.zip

# 4. 真实浏览器端到端业务验收（使用现有校验和第一字段，单行命令执行）
$buildSha = (Get-Content dist/galgame2voice-v3.1.1-windows-x64.zip.sha256).Trim().Split()[0]
uv run --frozen --extra desktop python scripts/smoke_windows_turn_reply.py --archive dist/galgame2voice-v3.1.1-windows-x64.zip --expected-sha $buildSha --scratch .local/smoke-turn-reply --with-browser-ui
```

---

## 5. 本地验收实测证据与验证边界

### 5.1 实测通过指标
- **后端测试套件**：3720 passed, 2 skipped, 55 requires_character_assets deselected, 2 websockets 弃用告警；所执行的 3720 项测试用例全部运行至终态，未出现未捕获异常或线程死锁。
- **前端测试套件**：全量 `npm test` 退出码 0，耗时约 22.80 秒。
- **便携包真实验收**：Windows x64 ZIP（58,447,742 字节）在真实桌面 Python 环境下 build、verify、base smoke 及同一 ZIP 启动真实 Chromium 执行设置中心、单轮对话（1 条用户提问 + 1 条助手回复，SQLite 精确落库 2 条）、LOG 语音 HTTP 200 取回、导出备份与重启关联验证全部 PASS（`result.json` `passed=true`）。

### 5.2 局限性与替身边界（真实环境需实际验证）
- **商业模型 API**：所有自动化门禁均基于本地替身与桩服务（Stand-ins），未连接真实的 OpenAI/Claude 商业 API 计费端点。
- **真实 GPU 与听感**：未在真实物理 GPU 硬件上加载 GPT-SoVITS 权重进行推理显存压测；LOG 音频通过 HTTP 200 取回仅验证了网络端点与文件格式，未评测真实人耳听觉自然度与声学品质。
- **Telegram 真实网络**：未进行跨公网 Telegram Bot 真实长轮询与网络通信测试。
- **视觉素材**：未加载与测试真实第三方大型人物立绘或头像素材（使用空角色/替身素材验证）。

---

## 6. 既有未合并 PR 关系说明 (PR #284, #286, #287)

当前仓库中尚有如下 3 个处于开启（OPEN）状态的独立分支 PR，**均不属于本次 v3.1.1 合并范围**：

1. [**PR #284 (Draft)**: `fix(adv): offer voice regeneration on legacy LOG replies`](https://github.com/LevenST24/galgame2voice/pull/284)
   - 分支：`fix/legacy-reply-regenerate`
   - 范围：为缺少分句的旧记录提供语音重生成入口，并将过短的16秒合成等待放宽至10分钟。
2. [**PR #286**: `fix(voice): match adaptive emotion references by canonical emotion`](https://github.com/LevenST24/galgame2voice/pull/286)
   - 分支：`fix/emotion-ref-keys`
   - 范围：通过规范情绪名/同义词映射自适应情绪参考音频，并支持自定义情绪标签。
3. [**PR #287**: `fix(adv): pick up a line repaired in another tab`](https://github.com/LevenST24/galgame2voice/pull/287)
   - 分支：`fix/two-tab-repair-sync`
   - 范围：处理 LOG 语音在另一标签页已修复时的 409 冲突并重新读取服务端音频（非设置双标签页）。

> [!NOTE]
> **后续维护者指引**：
> - 本次 v3.1.1 候选虽然修改了部分相同文件，但**并未证明**上述 PR 所针对的具体问题已被解决；**严禁将它们标记为已废弃或已被替代**。
> - 后续维护者需对上述 PR 的有效性进行独立评估。在未来的合并流程中，必须基于合入后的最新 `main` 分支分别执行 rebase，保留其有效改动，解决潜在的代码重叠（如 `frontend/src/controllers/line_audio_recovery.js`、`frontend/src/main.js`、`frontend/src/session_api.js`、`frontend/src/adv/backlog.js` 或 `galgame2voice/services/voice_resolver.py`、`galgame2voice/services/character_manager.py`），重新构建并部署前端静态产物，且在 CI 完全通过后方可合入。

# 后续迭代清单

检查基线：`abe97cc`（2026-11-04，PR #288 合并后），结合当前源码、发布工作流与尚未合并的 PR 整理。
优先级表示建议处理顺序；“已确认”表示有代码证据，“待验收”不等于已经发现缺陷。每项应独立修复、回归和交接。

## 优先处理的使用问题

- [x] **T01 / P1：修正 AI 自适应语音的情绪参考音频。**
  - 证据：[`voice_resolver.py`](../../galgame2voice/services/voice_resolver.py) 的 `DEFAULT_EMOTION_KEYS` 使用旧别名，`get_emotion_ref` 直接按输入取键，预解析字典也用查询别名存储；标准 `happy`、`sad` 等输入因此可能退回默认音频。`character_manager.py::_select_emotion_config` 优先规范名，也可能遮住清单中的自定义精确键。
  - 工作：适配 [PR #286](https://github.com/LevenST24/galgame2voice/pull/286) 至当前主分支，保留新加入的参考音频归属、路径与提示文本/语言约束。
  - 验收：标准情绪、别名、自定义清单键分别命中正确参考；缺失或无效参考安全回退；同一角色的音频、提示文本、语言一起变化；通过真实 TTS 请求边界的合成桩断言。真实听感另行验收。
  - 本次收口：原代码的 5 项回归全部失败；适配后加上无 `gentle` 时的真实回退身份用例，6 项通过，13 个相关测试文件共 274 项通过、16 项素材测试排除。最终合并与发布验证见 v3.1.2 Release 的运行链接。

- [x] **T02 / P1：接收另一个标签页已经完成的单句语音修复。**
  - 修复前证据：[`line_audio_recovery.js`](../../frontend/src/controllers/line_audio_recovery.js) 的请求异常路径直接重抛 409，未重新查询已提交音频。重新打开 LOG 仍可能读取本页旧内存。[PR #287](https://github.com/LevenST24/galgame2voice/pull/287) 提供了修复方向。
  - 工作：遇到 409 后只读查询服务端；核对会话、消息、行文本和当前操作归属，确认后更新本地记录。不要自动重发合成 POST。
  - 验收：两标签页修复同一句，后到的一页可获取先提交的结果；切换会话、取消、文本变化、查询失败均不覆盖其他记录；刷新后音频仍存在。
  - 本次收口：409 发生且消息已持久化时，只读发起 1 次消息历史 GET，严格校验会话标识、助手消息 id 与角色、行文本、就绪单 chunk 音频及音色，更新台词并同步元数据，在 LOG 提示「这一句已在另一网页修好，已更新」（本地缓存失败时提示未更新）并直接允许播放；若远端数据不可用或拉取失败则保留原音频与原始 409 错误，若本地归属变更或操作取消则抛出 AbortError 中止。前端 42 个单测用例全部通过；通过 Chromium 隔离运行器执行双标签页经服务端屏障同步的真实浏览器回归，断言一页直接修复（200）、另一页 409 后拉取远端音频并播放（覆盖冷缓存 1-2 次 GET 与温缓存 0 次 GET 且均真正播完 ended），未二次发起合成 POST（注：浏览器测试在桩服务上验证前端并发同步，后端 CAS 由独立集成测试覆盖；真实 GPU 与听感未验证）。

- [x] **T03 / P1：补齐旧格式聊天记录的语音恢复流程。**
  - 证据：[`backlog.js`](../../frontend/src/adv/backlog.js) 的 `renderAssistantLegacyEntry` 与现代分句走不同路径；[`audio_player_ui.js`](../../frontend/src/controllers/audio_player_ui.js) 仍有 16 秒合成等待。缺少 `lines_json` 的历史数据库消息也不满足当前持久化修复条件。
  - 工作：评估 [PR #284（草稿）](https://github.com/LevenST24/galgame2voice/pull/284)；将旧记录的显示、生成时限、服务端存储和取消行为一起收口。不能只增加按钮就宣布兼容完成。
  - 验收：超过七天、音频缺失、无 `lines_json`、慢引擎四种场景均有清晰操作；失败保留原记录；成功后重启与历史分页仍能播放；不猜测服务器消息编号。
  - 本次收口：后端 `line_audio.py`（`POST /api/voice/regenerate-line`）支持对 `lines_json IS NULL` 的旧版消息校验消息和会话编号、角色与日文台词一致后派生规范第 0 句，在合成前与入库前严格校验 `ReplyLineRecord` 规格（超长内容安全返回 422 且保留原音频与文本，10000 字符边界正常往返），合成前采集包含 content/audio_url 的快照，在 `immediate_transaction` 校验 snapshot CAS，原子写入单句 `lines_json` 并清空旧 `audio_url`；严格区分 NULL、损坏 JSON（返回 422 且保留原损坏数据）与空数组（返回 404）；前端 `line_audio_recovery.js` 与 `backlog.js` 接入 600 秒长耗时生成预算、双击保护（<500ms）与取消控制，兼容仅含 chunk/URL 结构的历史旧音频播放，对显式空数组 `lines: []` 消息保留文本与音频但隐藏不可达的修复操作（不扩大控制器适用边界），通过真实音频控制器调度使确认丢失音频（404/410）安全进入失效状态并显式展示修复操作（不静默调用 16 秒未持久化合成），对临时网络抖动与超时保持重试能力而不误禁用，在旧版音频过期（>7天）或失效时展示「用当前音色重新生成」，成功后安装规范第 0 句并写入展平播放元数据，即时允许在 LOG 中点击播放，且支持 409 只读远端同步；Python 单元/集成测试（47 个测试全部通过）、前端控制器与 LOG 单测（58+41 个测试全部通过）、Chromium 隔离回归（4 个旧版场景含真实 HTTP 404 缺失文件到原生 fetch 修复播放全流程、4 个现代场景全部通过且 Web Audio playLine 真实播完 ended）均已验证通过；真实 GPU 生成与听感保持在 T13 持续开放。

## 架构与性能

- [x] **T04 / P2：按业务职责拆分全局设置控制器。**
  - 证据：[`global_settings.js`](../../frontend/src/controllers/global_settings.js) 曾约 1,366 行，同时持有提供商表单、模型测试、运行状态轮询、引擎重启、Telegram 和全局保存的状态及监听器。
  - 工作：先拆提供商草稿与连接测试，再拆运行状态/引擎控制；保留一个设置窗口和入口协调器。让每个控制器明确拥有订阅、取消信号和关闭时的释放，不统一抹掉已有的细分版本守卫。
  - 验收：切页/关窗/重新打开/修改草稿/迟到请求的现有回归保持通过；真实浏览器验证焦点、返回首页和未保存草稿；每次仅迁移一个职责，不同时重做视觉。
  - 本次收口：
    - Phase 1（已完成，PR294）：已将提供商表单/草稿/读取状态、模型与密钥渲染、连通性测试、保存与启用逻辑抽离至实例拥有的 `ProviderSettingsController`（`frontend/src/controllers/provider_settings.js`），保留原有页脚保存预约、细分版本守卫与草稿隔离语义。
    - Phase 2（最终收口）：完成全部剩余业务控制器的生命周期解耦，将遥测轮询与硬件诊断卡片渲染抽离至 `TelemetrySettingsController`（`telemetry_settings.js`），引擎精度自动保存/切换/重启及离线缓存清理抽离至 `EngineSettingsController`（`engine_settings.js`），Telegram 开关/可见性/验证与测试逻辑抽离至 `TelegramSettingsController`（`telegram_settings.js`）；`global_settings.js` 收敛为纯粹的公共兼容门面与配置草稿协调器；每个控制器实例独立拥有 AbortController、代数守卫与事件解绑；`waitForEngine` 接入 AbortSignal 退出等待且不影响服务端重启；`initSettingsTabs` 与 `initEngineDirectory` 提供清理接口并在重新初始化时彻底释放旧监听器；消除了旧 Telegram 测试提前解锁新按钮、旧重启全局阻塞新操作、以及重复初始化堆叠监听器的缺陷；全部 17 项真实 Chromium 浏览器回归与 Node 全套回归（含新增 `test_settings_lifecycle_decomposition.mjs`）验证通过。

- [ ] **T05 / P2：缩小流式协调器的职责边界。**
  - 证据：[`stream_coordinator.py`](../../galgame2voice/services/chat_pipelines/stream_coordinator.py) 约 1,263 行，覆盖上游解析、TTS 队列、SSE、快照、拼接、数据库提交和好感度结算。
  - 工作：优先提取纯粹的快照状态计算和最终持久化策略；统一由现有轮次生命周期拥有任务与清理。先写清完成、截断、取消、断网的终态，再移动代码。
  - 验收：同一轮只保存一次、只结算一次；中断仍保留有效文本和已完整生成的音频；断连任务全部收束；不新增竞争读取 ASGI `receive()` 的任务，也不改变既有音色隔离。
  - 阶段进展：
    - 第一阶段（[PR #400](https://github.com/LevenST24/galgame2voice/pull/400)）：新增无 I/O 纯函数模块 [`stream_snapshot.py`](../../galgame2voice/services/chat_pipelines/stream_snapshot.py) 与单测 [`test_stream_snapshot.py`](../../tests/test_stream_snapshot.py)，将单句音频状态结算（`new_line_audio_state`、`build_line_audio_event`、`resolve_line_audio_snapshot_status`、`snapshot_lines_with_audio`）、完成/截断/取消/断网四类终态的快照状态计算（`compute_completion_snapshot`、`compute_early_exit_snapshot`、`compute_disconnect_snapshot`）、提前退出 `done` 事件构造（`build_early_done_event`）及切片/TTS 参数整理从 [`stream_coordinator.py`](../../galgame2voice/services/chat_pipelines/stream_coordinator.py) 中抽离。
    - 已覆盖验收：以纯函数单测明确了正常完成、异常截断、用户取消与客户端断网四类终态下的有效文本判定与已完成单句音频保留/未完成句标 `failed` 规则；委托切换后不新增竞争读取 ASGI `receive()` 的任务，也不改变既有音色隔离。
    - 剩余未完成：终态数据库落库、主音频 WAV 拼接计划与好感度结算的最终持久化策略仍在 [`stream_coordinator.py`](../../galgame2voice/services/chat_pipelines/stream_coordinator.py) 内联（同一轮只保存一次、只结算一次仍依赖协调器实例状态）；后台生产、合成与取消监听任务的生命周期及断连收束清理尚未统一收口；客户端断网兜底（`compute_disconnect_snapshot`）在仅有 `announced_lines` 而 `partial_chinese`/`partial_japanese` 为空时的部分落盘判据尚未与 `compute_early_exit_snapshot` 对齐。

- [ ] **T06 / P2：让 SSE 和恢复接口契约可以被前后端共同核验。**
  - 证据：[`ai.js`](../../frontend/src/ai.js)、[`conversation.js`](../../frontend/src/conversation.js)、后端流式协调器及轮次恢复接口分别维护事件名、字段和身份判定。
  - 工作：从已有事件选小范围建立版本化样例/契约测试，先覆盖 `turn_started`、`done`、`line_audio` 和 `/api/chat/turn-reply`。无需为此整体迁移 TypeScript 或引入前端框架。
  - 验收：服务端输出样例由真实序列化器生成，前端消费同一样例；缺字段、冲突 ID、乱序、重复和未知事件有明确行为；修改协议时双方测试一起失败或一起更新。
  - 阶段进展：
    - 第一阶段（[PR #402](https://github.com/LevenST24/galgame2voice/pull/402)）：新增版本化共享契约样本 [`sse_turn_reply_v1.json`](../../tests/contracts/sse_turn_reply_v1.json)（`schema_version: "sse_turn_reply_v1"`）及后端 [`test_sse_contract_samples.py`](../../tests/test_sse_contract_samples.py)、前端 [`test_sse_contract_samples.mjs`](../../tests/test_sse_contract_samples.mjs) 双端契约测试，打通后端 [`sse.py`](../../galgame2voice/utils/sse.py) `format_sse_frame`、`sse_event_formatter`、`StreamCoordinator`、`get_turn_reply` 与前端 [`ai.js`](../../frontend/src/ai.js)、[`conversation.js`](../../frontend/src/conversation.js)、`resolveTurnReplyIdentity`、`createLineAudioRecovery` 对同一样本的生成与消费核验。
    - 已覆盖验收：覆盖 `turn_started`、`line`、`audio_chunk`、`line_audio`、`done`（完整与截断）及 `/api/chat/turn-reply` 全部五种状态（`ready` / `pending` / `gone` / `ambiguous` / `unavailable`）的真实序列化输出与前端同源消费，并锁定了缺字段、冲突 ID、乱序、重复和未知事件五类边界在当前实现下的行为；破坏上述已覆盖字段或状态时双端测试会同步失败。
    - 剩余未完成：[`conversation.js`](../../frontend/src/conversation.js) 在 `line_audio` 早于同句 `line` 乱序到达时未缓存回填该句音频状态（当前仅靠终态 `done.lines` 兜底修正），且对 `line_audio` 缺少合法行号或 `status` 尚未做与 [`ai.js`](../../frontend/src/ai.js) `isReplyProgress` 一致的防御校验；其余流式事件（`text`、`progress`、`voice_job`、`error`、`audio_chunk_error`）尚未纳入版本化共享契约。

- [ ] **T07 / P2：测量用户实际听到第一句的等待时间。**
  - 证据：后端已有 `MetricsCollector`、TTFT、首个 TTS chunk 和排队均值。服务端生成首片段的时刻不等于浏览器开始播放的时刻。
  - 工作：复用 [`metrics_collector.py`](../../galgame2voice/services/metrics_collector.py) 和 [`audio_player.js`](../../frontend/src/audio_player.js)，用单轮时间线区分首字、首句、排队、合成、下载/解码、真正播放；样本有上限且不记录正文或密钥。
  - 验收：冷启动、热缓存、慢合成和网络抖动分别记录分布与环境；先拿到基线再决定缓冲或并行策略，不用固定 sleep 或模拟引擎成绩宣称真实提速。
  - 阶段进展：
    - 第一阶段（[PR #401](https://github.com/LevenST24/galgame2voice/pull/401)）：在 [`ai.js`](../../frontend/src/ai.js) 透传 SSE `done` 事件中的 `telemetry`（来自后端 [`profiler.py`](../../galgame2voice/utils/profiler.py) `ChatTurnProfiler`）与 `latency_ms`，并在 [`adv_app.js`](../../frontend/src/adv/adv_app.js)、[`main.js`](../../frontend/src/main.js) 与 [`index.html`](../../frontend/index.html) 诊断面板 `#g-dash-perf-summary` 中对接 [`audio_player.js`](../../frontend/src/audio_player.js) `StreamAudioController.playLine` 的 `onStart` 回调，建立容量上限为 5 轮的内存环形缓冲 `recentTurnTimelines`，配套单测 [`test_first_audio_timeline.mjs`](../../tests/test_first_audio_timeline.mjs)。
    - 已覆盖验收：单轮时间线已记录请求发起、首字（`llm_first_token_ms`）、首句切出（`first_sentence_parsed_ms`）、首音就绪（`first_tts_ready_ms`）、浏览器真正开播（`first_audio_play_ms`）与整轮耗时（`total_ms`），覆盖首句无声跳至后续有声行、`done` 与 `onStart` 乱序到达、最多保留 5 轮上限且不记录对话正文或密钥、不发额外遥测请求。
    - 剩余未完成：TTS 排队、合成、网络下载与 Web Audio 解码等细分阶段耗时拆分及 [`metrics_collector.py`](../../galgame2voice/services/metrics_collector.py) 汇聚/环境标签记录尚未补齐；后端 [`profiler.py`](../../galgame2voice/utils/profiler.py) 在整轮无音频生成时仍会将 `t5_app_ttfa_ms` 回退记为整轮耗时；冷启动、热缓存、慢合成和网络抖动四类场景下的真实环境分布基线采样及后续缓冲/并行策略评估**待维护者验收**。

## 测试与发布维护

- [x] **T08 / P1：使依赖审计失败真正阻断合并。**
  - 证据：[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) 的 `dependency-audit` 设置 `continue-on-error: true`，与交接文档的六项检查全成功要求不完全一致。
  - 工作：取消允许失败；如需要例外，只针对已分析的具体漏洞建立有负责人和到期日的记录，不能永久放过整个作业。
  - 验收：受控失败能让整次 CI 失败；正常 Python/npm 审计通过。区分审计服务不可用与实际漏洞，不用重试掩盖真实结果。
  - 本次收口：移除作业级 `continue-on-error`，保留原 Python/npm 审计命令、阈值和固定 action 版本；真实执行结果见发布修复 PR 的 CI。尚未注入漏洞或人为失败来进行负向演练，该项边界不等于现有依赖存在漏洞。

- [ ] **T09 / P2：把关键便携版回归提前到合并前。**
  - 证据：[`release.yml`](../../.github/workflows/release.yml) 已有真实 EXE 和 Chrome 业务验收，但仅由手动执行或版本标签触发；常规 PR CI 不执行该工作流。
  - 工作：复用现有流程，为启动器、打包、数据库迁移等相关改动添加有界的合并前检查；最终发布仍运行完整便携包验收。复用 workflow，避免复制一套会漂移的命令。
  - 验收：能在 PR 中发现缺少运行文件、启动失败或打包后恢复失败；纯文档变更不重复构建大包；失败上传现有诊断，成功后才允许正式发布。

- [x] **T10 / P2：修复测试能力探测遗留临时目录。**
  - 证据：[`tests/conftest.py`](../../tests/conftest.py) 的 `_probe_writable_private_temp_dir` 创建 `g2v_cap_*`，写入后成功或失败均未清理。
  - 工作：用 `TemporaryDirectory` 或可靠的 `finally` 管理本次创建的目录；禁止按名字批量删除工作区中已有目录。
  - 验收：可写、写失败与不可创建三种探测结果保持原含义，执行后仅本次目录被清理；重复收集测试不会持续制造垃圾目录。
  - 本次收口：探测在 `finally` 中用 `shutil.rmtree` 删除本次 `mkdtemp` 创建的目录，不按名字匹配任何已有目录；新增 [`test_capability_probes.py`](../../tests/test_capability_probes.py) 覆盖可写、写入失败、无法创建三种结果（修复前前两项失败）。修复前 conftest 会以 `conftest` 和 `tests.conftest` 各导入一次，每次 pytest 运行新增两个 `g2v_cap_*` 目录；系统临时目录不可写时落在当前目录（仓库根目录，因此 `.gitignore` 有 `g2v_cap_*/`）。修复后同样的运行不再新增目录。已有的遗留目录没有自动清理。

- [ ] **T11 / P2：整理测试入口和模块归属。**
  - 证据：[`frontend/package.json`](../../frontend/package.json) 的 `test` 是逐个文件维护的长命令；测试中仍有 `adversarial_m*`、`challenger` 等历史流程命名。
  - 工作：先建立明确的前端测试发现规则和排除清单，再按被测模块渐进改名/归档。避免一次重写全部测试或只为减少行数删除边界回归。
  - 验收：列出迁移前后同一测试集合；新增符合约定的测试自动执行；独立浏览器/素材/集成任务仍可单独运行；关键失败不会因 glob 或退出码处理漏掉。
  - 阶段进展：
    - 第一阶段（[PR #399](https://github.com/LevenST24/galgame2voice/pull/399)）：新增 [`run_tests.mjs`](../../frontend/run_tests.mjs) 与自检回归 [`test_frontend_test_discovery.mjs`](../../tests/test_frontend_test_discovery.mjs)，将 [`frontend/package.json`](../../frontend/package.json) 的 `npm test` 改为顶层 `tests/test_*.{mjs,js}` 按 ASCII 顺序自动发现（`discoverFrontendTests`）+ 显式冻结排除清单（`EXCLUDED_TESTS`）与临时探测前缀过滤（`TEMP_TEST_PREFIX_RE`）+ 逐文件独立 Node 子进程隔离执行。
    - 已覆盖验收：核对了迁移前 61 个前端测试集合零遗漏（合并时含自检共 62 项，后续新增的合规前端测试已自动纳入执行），独立浏览器/素材/集成脚本与 `tests/helpers/` 保持隔离可单独运行，子进程非零退出码、信号中断与空目录发现失败均严格透传并短路阻断。
    - 剩余未完成：`tests/` 下历史遗留的 `adversarial_m*`、`challenger`、`m*` 等里程碑/流程化测试文件（含前端 `test_m7_audio_lru_challenger2.js` 与后端 `test_adversarial_*` 等）尚未按被测业务模块渐进改名与归档。

- [ ] **T12 / P2：为内部界面建立可重复的浏览器视觉验收。**
  - 证据：现有发布 smoke 验证设置 Tab、LOG、导出和音频地址等行为，尚不能证明立绘部件对齐、排版比例或所有滚动/遮挡场景符合审美。
  - 工作：以已认可的首页比例为参照，使用可公开分发的合成分层立绘和固定对话，覆盖舞台、设置、LOG 与错误/加载态；先确定截图和设计标准，再逐页改视觉。
  - 验收：1920×1080、1366×768、窄窗口及系统缩放下，正文、按钮、底部菜单可见可操作；表情/口型定位稳定；人工检查差异。禁止把“截图生成成功”当作视觉通过。

- [ ] **T13 / P1（发布验证）：补真实供应商、GPT-SoVITS 和 Telegram 验收。**
  - 证据：[`RELEASE_HANDOFF_3_1_1.md`](RELEASE_HANDOFF_3_1_1.md) 已明确现有商业 API、GPU/听感、真实 Telegram 和第三方立绘的验证空白。
  - 工作：由维护者在专用测试安装中提供允许使用的服务和资源，记录配置类别、硬件、版本和结果；不把开发机真实聊天、密钥或素材复制进 CI。
  - 验收：至少完成一轮真实文字流、连续逐句语音、角色切换、停止/重试、离线恢复与 Telegram 重置。没有环境时明确保留“未验证”，不阻塞无关代码维护，也不虚报通过。

- [x] **T14 / P1：在执行便携版业务脚本前确认浏览器导航就绪。**
  - 证据：v3.1.1 发布工作流两次在 `Runtime.evaluate` 报 `Execution context was destroyed`；截图仍处初始加载阶段，而后端回复关联、重启与修复音频校验已通过。
  - 工作：修复 [`smoke_windows_ui.py`](../../scripts/smoke_windows_ui.py) 的初始导航等待，在既定时限内仅重试只读就绪检查，业务脚本执行一次。
  - 验收：初始上下文销毁、URL 不匹配、永不就绪、业务阶段上下文销毁均有协议边界回归；最后在正式 v3.1.2 Windows ZIP 上完整验收。历史失败日志保留，不能靠反复重跑宣告修复。
  - 本次收口：初始导航、非暂态协议错误、错误路径、持续加载与业务仅执行一次均有回归；独立运行驾驶器与界面组合 20 项通过、3 个子用例通过。最终冻结包结果记录在 v3.1.2 Release，正式测试不依赖历史 Git 提交。

## 执行与交接

建议顺序：T01 → T02 → T03；T08/T10 可独立小 PR；随后按 T07 的测量结果选择性能工作，再逐项推进 T04–T06。T09/T12 是防止体验回归的持续工程，T13 需要真实测试环境。

每项交接须记录触发条件、修复前失败/修复后通过的证据、涉及接口与兼容性、实际执行的验证及剩余边界。完成项可勾选并补 PR 链接，未完成项不得随着版本发布自动清空。

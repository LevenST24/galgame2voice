# AGENTS.md（前端指南：纯 ADV 界面架构）

本文件为开发与维护 `frontend/` 纯 ADV 界面提供架构地图与技术约定。

---

## 依赖与环境

- **构建与开发工具**：Vite ^6（开发服务器端口 `3015`，基础路径 `base: '/static/'`，构建输出至 `dist/`）。
- **运行时环境**：纯原生 JavaScript (ES Modules)，无前端框架，无第三方运行时 npm 依赖。

---

## 模块地图 (`frontend/src/`)

应用以 `index.html` 为宿主骨架，入口自 `src/main.js` 启动。模块依赖链路如下：

### 1. 核心与数据流
- `src/main.js`：应用入口，协调会话同步、鉴权拦截、全局与会话设置控制器，并将 `conversation` 控制器挂载至 ADV 舞台。
- `src/store.js`：前端状态管理与 `localStorage` 持久化（Key: `gal2voice.chat.v1`，包含会话列表 `sessions`、活跃会话 `activeId` 及全局设置 `global`）。
- `src/conversation.js`：无 DOM 依赖的会话生命周期控制器，管理单轮回复状态机（`turnstart`、`text`、`line`、`line_audio`、`audio_chunk`、`done`、`cancelled`、`error`），控制发送、停止（`stop`）与行内重试（`retry`）。
- `src/ai.js`：底层流式客户端，通过 `fetch` 发起 POST `/api/chat/stream` 并解析 Server-Sent Events (SSE)。
- `src/session_api.js`：与后端 SQLite 同步会话列表及历史分页（`/api/chat/sessions`、`/api/chat/history`）。
- `src/session_data.js`：会话数据清洗与白名单校验，包括 `pickSettings`、`validAdvSetting`、`normalizeLines` 与快照序列化。
- `src/auth.js`：控制台访问令牌鉴权拦截器（SessionStorage Key: `galgame_console_token`，自动注入 Bearer Token，处理 401 弹窗）。
- `src/request.js`：带超时的 JSON 请求 `requestJson`，按 HTTP 状态和连接错误给出中文提示（`statusMessage`、`connectionMessage`）。
- `src/api.js`：设置相关接口（模型厂商、全局配置、版本检查与更新）；写请求有时限，失败后不自动重发。
- `src/chat_backup.js`：完整聊天备份：导出数据库与浏览器里的记录（`exportCompleteChatBackup`），导入时合并去重（`mergeChatBackup`）。

### 2. 语音与音频
- `src/voice.js`：浏览器原生语音能力。发送端使用 `SpeechRecognition`（中文转写，要求 Chrome / Edge）与 `MediaRecorder`（录音暂存）；提供 `BoundedAudioStore`（LRU 30 项硬上限，自动调用 `URL.revokeObjectURL` 释放 Blob 内存）。
- `src/audio_player.js`：`StreamAudioController`，基于 Web Audio API 的流式无缝音频播放器。支持 12ms 瞬态防爆微渐变（Micro-fade）、40ms 平滑线性渐出打断（Smooth Linear Fade-out Interrupt）、单句排期播放（`playLine` / `stopLine`）、自适应 Jitter 缓冲以及通过 `getRms()` 计算均方根振幅驱动立绘口型同步。
- `src/cache.js`：音频两级缓存（Web Cache Storage API + 内存 LRU），支持音频 Blob 持久化与秒开重播。
- `src/audio_request.js`：`withAudioDeadline`，给音频请求连同读取、解码设一个总时限（默认 15 秒），可随播放一起取消。`audio_player.js` 也用它限制 `AudioContext.resume()` 的等待（1.5 秒）。

### 3. ADV 舞台与组件 (`src/adv/`)
- `adv_app.js`：ADV 舞台统一装配与挂载入口（`mountAdv`），管理舞台生命周期、尺寸更新、背景同步、历史会话抽屉及各子组件协同。
- `stage.js`：1920×1080 逻辑舞台画布容器（`createStage`）。通过 CSS 变量 `--u` 统一缩放（`--u: min(100vw / 1920, 100dvh / 1080)`，所有窗口比例均使用 16:9 舞台）。管理 0 至 60 级分层（Shell → Background → Sprite → Fx → Textbox → QuickMenu → Overlay）。
- `textbox.js`：对话文本框（`createTextbox`）。包括半透明渐变底板（`.adv-band`）、立绘面部视口（`.adv-face-slot`）、角色姓名牌（`.adv-name-plate`）、中日双语字幕列、思考动画指示器以及用户文本输入框（`.adv-player-input-wrap`）。
- `sprite_state.js`：无 DOM、无计时器的立绘状态纯函数：解析立绘清单（`normalizePortrait`）、按情绪选脸（`facePool`、`pickFace`）、脸红分级（`blushTier`）、组合分层（`resolveSpriteState`）和演出限频（`createActingGate`）。选状态的逻辑放这里，`sprite.js` 只管绘制和计时。
- `sprite.js`：立绘渲染核心（`createSpriteView`）。支持全图立绘与 v3 分层立绘（`layers.json`），实现 A/B 缓冲区平滑过渡、眨眼机制（`BLINK`）、基于音频 RMS 的口型同步（`LIP_SYNC`）以及 96MB 内存预加载控制。
- `transitions.js`：基于 Web Animations API 的立绘平滑切换动效。
- `line_player.js`：无 DOM 台词播放状态机（`createLinePlayer`）。精确调度单句台词展现、打字机逐字间隔、语音同步、AUTO 自动推进与 SKIP 快速跳过。
- `quick_menu.js`：舞台底部快捷菜单（`createQuickMenu`），集成 AUTO、SKIP、LOG、VOICE 重播、HIDE 隐藏文本框、CONFIG 快速设置、背景切换及模型状态指示灯。
- `shortcuts.js`：全局键盘与手势控制器（`createShortcuts`）。处理 Enter / Space 推进、右键隐藏文本框、滚轮向上呼出 LOG、Esc 关闭面板/停止生成、长按 Ctrl 临时快进。
- `backlog.js`：LOG 历史记录抽屉（`createBacklog`），展示完整对话历史，支持单句语音与用户录音重播、历史记录向前分页加载。
- `config_panel.js`：全屏半透明 ADV 交互设置面板（`createConfigPanel`），支持文字速度试打样、窗口透明度、语言切换等即时预览与还原。
- `background.js` & `bg_panel.js`：舞台背景及时间差分选择器（白天 / 傍晚 / 夜晚 / 自动跟随时间），支持作品分包。
- `actions.js` & `title_hook.js` & `title.js` / `title.css`：透明启动首页，围绕舞台当前会话显示中日摘录、试听、最近会话预览、全部会话分页、新对话选择器与首次使用引导。数据/操作只经 actions，进入对话用 300ms 淡出，`previewSession` 保持首页且不聚焦输入框。
- `portrait_cache.js` & `replay.js`：首页与舞台共用签名立绘清单缓存/裁脸方案；首页与 LOG 共用单句语音可播规则（含七天有效期）。首页状态打开时、每 5 秒及设置关闭后刷新，关闭时清理计时器和订阅。
- `paginate.js` & `graphemes.js`：基于字符簇（Grapheme Clusters）的文本分页折行算法，避免行末禁则与字符撕裂。
- `sprite_fx.js`：角色头顶情绪特效 SVG 标记（出汗、生气、爱心、闪光、问号、叹号）。
- `settings.js`：ADV 设置项校验与字段规范。

### 4. 控制器与弹窗 (`src/controllers/`)
- `global_settings.js`：全局设置弹窗（`#globalModal`，包含运行状态、语音服务、对话模型、Telegram、角色与记忆、备份与更新 6 个 Tab）。
- `voice_settings.js`：会话设置弹窗（`#sessionModal`，配置当前会话的角色人设、音色绑定、LLM 采样参数及 TTS 推理参数）。
- `audio_player_ui.js`：输入端麦克风录音控制与播放器 UI 状态协调。
- `session_backup.js`：完整聊天备份与浏览器本地快照的导出与导入。
- `update_manager.js`：版本检查与在线更新管理。
- `engine_directory.js`：「语音服务」页里本机 GPT-SoVITS 安装目录的选择与保存（保存目录和启动引擎是两步）。
- `engine_recovery.js`：`waitForEngine`，轮询 `/api/system/engine-status` 直到引擎就绪（默认最多 3 分钟）。
- `config_recovery.js`：`saveConfirmedConfig`，保存全局配置；响应丢失时用一次 GET 核对是否已经写入，不重发写请求。

### 5. 共享界面工具 (`src/`)
- `ui.js`：会话列表渲染（`renderSessionList`，「会话」抽屉里用）与提示条（`showToast`）。
- `settings_ui.js`：设置弹窗和令牌弹窗的打开与关闭（`openModal`、`closeModal`）、焦点与键盘处理、标签页切换；`onModalChange` 让 ADV 舞台在弹窗打开时暂停。
- `model_badge.js`：`getModelStatus`，算出快捷菜单状态点显示的模型与音色状态。

---

## SSE 事件流规范 (`POST /api/chat/stream`)

`src/ai.js` 消费服务端返回的标准 SSE 流，涉及以下命名事件：

| 事件名 | 载荷内容 | 说明 |
| :--- | :--- | :--- |
| `text` | `{ "delta_chinese": "...", "emotion": "..." }` | 中文增量文本片断，用于流式累加展示。 |
| `line` | `{ "index": 0, "zh": "...", "ja": "...", "emotion": "...", "act": {} }` | 单句台词解析就绪事件，包含对齐的文本与演出标记。 |
| `line_audio` | `{ "line": 0, "status": "ready"\|"skipped"\|"failed", "chunks": [...] }` | 某句台词的语音最终状态，按台词顺序发出。`skipped` 表示这句没有生成语音（例如只有省略号），前端直接出字、不等语音；`failed` 另带 `error`。 |
| `audio_chunk` | `{ "line": 0, "index": 0, "audio_url": "...", "sentence": "...", "emotion": "..." }` | GPT-SoVITS 合成的单个音频片段到达，用于预热和播放队列。能对应到台词时才带 `line`。 |
| `audio_chunk_error` | `{ "line": 0, "index": 0, "sentence": "...", "error": "..." }` | 单句音频合成异常提示，文字流继续推进，不打断整体对话。能对应到台词时才带 `line`。 |
| `error` | `{ "error": "...", "error_code": "..." }` | 生成过程中断的错误事件，附带结构化错误码与中文指引。 |
| `done` | `{ "chinese": "...", "japanese": "...", "chunks": [...], "lines": [...] }` | 本轮对话生成结束，携带最终完整文本、音频切片与行元数据。 |

---

## 设置面板位置

1. **ADV 交互设置（CONFIG 面板）**：
   - 入口：快捷菜单中的 **CONFIG** 按钮或按键 `F8`。
   - 包含：玩家名字、主副字幕语言、窗口不透明度、打字速度、AUTO 参数、服装、眨眼及口型动画开关；底部提供直达“对话模型设置”与“会话与角色设置”的导航按钮。
2. **会话与角色设置（Session Settings，`#sessionModal`）**：
   - 入口：CONFIG 面板底部的直达按钮、首页首次使用引导的「选一个角色」、全局设置「角色与记忆」页的「打开当前会话设置」，以及请求频率超限等非模型错误的「去设置」。
   - 包含：角色人设编辑（保存后写回角色包）、角色音色选择与扫描、新建自定义音色、大模型采样参数（温度、Top P、惩罚项、最大长度、上下文条数）、回复格式（默认 lines 逐句）以及会话级 TTS 推理参数。
3. **全局系统设置（Global Settings，`#globalModal`）**：
   - 入口：左上角工具栏「会话」抽屉底部的「全局设置」按钮、首页齿轮；首页状态胶囊的「去设置」分别打开「对话模型」/「语音服务」页；快捷菜单的模型状态点、CONFIG 面板的直达按钮，以及模型相关错误的「去设置」（文本框和 LOG 一样，按 `needsModelSettings` 判断）直接打开「对话模型」页。
   - 包含 6 个标签页：
     - **运行状态**（`gp-dashboard`）：GPT-SoVITS 连通性、计算精度切换与重启、硬件资源与缓存清空。
     - **语音服务**（`gp-inference`）：引擎地址、本机 SoVITS 目录选择、全局计算精度与缓存保留时长。
     - **对话模型**（`gp-models`）：全厂商模型卡片（API Key、Base URL、模型选择、思考强度、连通性测试与一键启用、允许私网 LLM 端点开关）。
     - **Telegram**（`gp-telegram`）：机器人 Token、管理用户 ID、代理配置与连通性测试。
     - **角色与记忆**（`gp-memory`）：长期记忆说明。
     - **备份与更新**（`gp-update`）：完整聊天备份导出/导入与版本更新拉取。

---

## 构建与部署

- **日常开发**：
  ```bash
  cd frontend
  npm run dev
  ```
  通过 Vite 开发服务器热更新，代理请求至 `http://127.0.0.1:8080`。

- **构建与产物部署**：
  ```bash
  cd frontend
  npm run deploy
  ```
  `npm run deploy` 顺序执行 `vite build` 与 `node deploy.mjs`：
  1. Vite 将前端产物打包至 `frontend/dist/`。
  2. `deploy.mjs` 自动清空 `galgame2voice/static/assets/` 下带有历史哈希的旧打包文件，保留 `characters/` 和隐藏文件，并将 `dist/` 完整同步到 `galgame2voice/static/`。
  3. **严禁手动直接复制 `dist/*`**，否则会导致历史哈希文件堆积产生漂移。

---

## 避坑经验与已知约束

- **Windows MIME 类型**：Windows 注册表常把 `.js` 关联为 `text/plain`，导致浏览器因 Strict MIME Type 拦截拒绝加载 ES 模块。后端已在 `galgame2voice/main.py` 启动阶段通过 `mimetypes.add_type("text/javascript", ".js")` 和 `mimetypes.add_type("text/javascript", ".mjs")` 强制显式修正，**切勿删除该逻辑**。
- **语音识别时序**：浏览器的 `SpeechRecognition` 通常在执行 `stop()` 之后才会触发 `onresult` 递交最终的识别文本，不要在 `stopListening()` 中强行重置该轮代际编号，否则会丢失识别内容。
- **AudioContext 唤醒机制**：没有用户手势时，浏览器让 `AudioContext` 保持 `suspended`，`resume()` 返回的 Promise 可能一直不完成。必须给它设时限（`audio_player.js` 用 `withAudioDeadline` 限 1.5 秒），不要直接 `await`，否则播放流程会卡住。

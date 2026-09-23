# AGENTS.md（本地版，适配 Galgame2Voice 后端）

## Dependencies
- vite ^6：构建与 dev server（端口 3015，base '/static/'，产物 dist/index.html）
- 无运行时 npm 依赖（原 Supabase 客户端已在本地化时移除）

## Architecture
- 纯原生 JS 多模块，无框架：`index.html` 骨架 + SVG sprite；`src/main.js` 事件绑定与渲染调度；`src/store.js` 会话状态（localStorage key: `gal2voice.chat.v1`，回退读取历史 inkwell key 兼容旧数据）；`src/ui.js` DOM 渲染；`src/ai.js` 本地后端流式客户端；`src/voice.js` 浏览器原生语音（SpeechRecognition 语音输入 / SpeechSynthesis 朗读兜底）；`src/styles.css` 全部样式与设计令牌（浅色主题，主色紫）。
- 聊天链路（`src/ai.js`）：POST `/api/chat/stream`，SSE 命名事件 `text`（`delta_chinese` 增量）/ `audio_chunk`（逐句 GPT-SoVITS 音频 URL）/ `audio_chunk_error`（忽略）/ `error` / `done`。请求携带会话设置覆盖：`system_prompt` / `temperature` / `max_context`（后端 ChatRequest 已支持，为空时走 Voice Profile 默认人设）。删除会话时前端同时调 `DELETE /api/chat/history?session_id=` 清理后端持久化历史。
- 语音链路：发送侧 = MediaRecorder 真录音（blob 存内存 `audioStore`，语音条可回放）+ SpeechRecognition 并行转文字；接收侧 = AI 回复携带 `audioUrls`（后端 TTS 分句缓存，路径如 `/audio/cache/xxx.wav`，内容为日文台词，匹配参考音频语言），播放器按顺序逐段播放并按 (i + 片内进度)/总段数 点亮波形；无音频时回退浏览器 SpeechSynthesis 并 toast 说明原因（旧消息或生成时引擎未就绪）。
- 设置分层：**会话设置**（右上角滑杆图标）= System Prompt、角色音色绑定（voiceProfileId）、采样参数（温度/Top P/频率惩罚/存在惩罚）、生成控制（max_tokens、上下文条数）、语音推理参数（tts_options：语速 speed/Top K/TTS Top P/TTS 温度，覆盖全局质量预设），全部随请求实时生效，后端 ChatRequest 已支持全部字段。**全局设置**（侧边栏齿轮）= 对话模型（`/api/providers` 列表 + `POST /api/providers/{id}/activate` 启用，含"＋自定义模型/接口"表单 OpenAI 兼容 Base URL/Key/模型名，id 固定 'custom'）、TTS 质量预设（存 localStorage `state.global.ttsPreset`，随 chat 请求 `preset` 字段发送）。人设预设 chips 已按用户要求删除。禁止把音色选择放回全局设置——用户明确要求"每个会话可以跟不同的人设/音色聊天"。
- 音色按会话绑定（`ensureSessionVoice`）：会话 settings.voiceProfileId 非空时，切换到该会话自动 `POST /api/voice/switch` 加载对应权重（toast 提示，失败回滚）。切换权重会触发 GPT-SoVITS 重新加载模型，系统内存不足时可能 OOM 崩掉引擎进程（之后所有 TTS 500），需重启 GPT-SoVITS 恢复。
- 会话内新建自定义音色：音色下拉含"＋ 新建自定义音色"项，展开表单从 `GET /api/voice/scan-models`（结果缓存 60s，前端亦有模块级缓存，创建成功后置 null 强制重扫）选择 .ckpt/.pth/参考音频，`POST /api/voice/profiles` 创建后立即绑定到会话并触发切换。注意 GPT-SoVITS 无参考音频会显著降低音色相似度。
- 构建部署：`npm run deploy`（= vite build + deploy.mjs 清理旧 assets 后整目录拷贝到 `../galgame2voice/static/`）。**禁止**手动 `cp -r dist/*`——会残留历史哈希产物。`/` 由 FastAPI 托管 index.html，`/static/` 挂载同目录。旧版 UI（index_legacy.html、settings.html、static/js、static/css）已删除（git 历史可找回）；旧版 settings.html 控制台已被用户明令禁止出现在 UI 中（"不要搞跳转"）——前端任何地方不得链接 settings.html，其全部设置已原生融入：系统状态行、对话模型、TTS 质量预设、音频缓存保留时长、Telegram（token/代理/测试连接/热重载保存）在全局设置；音色、采样、推理参数在会话设置。齿轮图标用 lucide settings 路径（旧的自画齿轮形似太阳被否决）。
- 开发模式：`npm run dev`，vite 代理 `/api`、`/audio`、`/ai` 到 `127.0.0.1:8080`。

## What Didn't Work
- ❌ Windows 下 Python mimetypes 跟随注册表把 .js 判为 text/plain，ES module 被 Strict MIME 拒载 → 已在 `galgame2voice/main.py` 顶部 `mimetypes.add_type("text/javascript", ".js")` 强制修正，勿删。
- ❌ 暗色鎏金主题 → 用户反馈不实用，已重做为主色紫浅色方案；勿再引入深色装饰元素。

## Lessons
- 顶栏徽章外层 span 必须带 `id="modelBadge"`（updateBadge 依赖该元素，缺失会在启动时抛 TypeError 中断首条消息渲染）。
- main.js `playAiVoice` 依赖 voice.js 的 `estimateDuration`，import 列表曾遗漏导致 ReferenceError，新增语音相关调用时检查导入完整性。
- 验证脚本（puppeteer-core + 系统 Chrome）判断"回复完成"要等 `#stopBtn` 隐藏，语音条在问候语上始终存在，不能作为流结束信号。

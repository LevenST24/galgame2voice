# 星光咖啡馆与死神之蝶 · 四季夏目 聊天 AI (Galgame2Voice)

> 中文显示 + 日文流式语音的 Galgame 女主聊天伴侣，基于 Python / FastAPI 构建，深度协同 **GPT-SoVITS**。

`galgame2voice` 是一个轻量级、高性能的 Python/FastAPI 伴侣扩展服务，无缝集成主流大语言模型与 **GPT-SoVITS v2** 语音合成引擎，扮演《星光咖啡馆与死神之蝶》（喫茶ステラと死神の蝶）角色「四季夏目」等 Galgame 角色与玩家进行实时双语互动：

- 🗣️ **双语流式输出**：大模型按流式 JSON 输出「中文台词 + 日文台词」
- 🀄 **低延迟中文渲染**：流式输出中文内容并在网页界面实时逐字呈现
- 🇯🇵 **分句并发语音合成**：日文台词按句自动切分，后台并发队列调用 GPT-SoVITS 合成高质量音频并流式播放
- 💬 **双通道交互**：提供现代化 Web 聊天界面（`/`）与 Telegram 机器人双向语音/文本互动
- 🎛️ **可视化管理控制台**：现代化 Web 配置后台（`/settings.html`），支持 10+ 家主流 LLM/STT 服务商管理、音色方案热切换、动态延迟测速与密钥脱敏保护
- 🛡️ **安全加固**：控制台访问 Token 认证（Bearer + 常量时间比较）、LLM 接口 SSRF 防护（私网/环回/云元数据网段拦截）、输入长度与参数范围校验、请求限流、密钥脱敏返回，默认 `127.0.0.1` 本地绑定。详见 [SECURITY.md](SECURITY.md)

---

## 核心架构与处理链路

```
[用户提问]
   │
   ▼
[FastAPI / LLM 流式引擎]
   │
   ├─► 增量中文流 (SSE: text) ─────────────────────► [前端 Web 界面即时显示]
   │
   └─► 日文分句检测 (StreamingBilingualParser)
         │
         ▼
     [异步并发队列 asyncio.Queue]
         │
         ▼
     [GPT-SoVITS 客户端 (Mutex 互斥合成)] ────────► [前端 Web Audio 队列连续播放]
```

---

## 支持的模型服务商 (10+ 预置模板)

内置主流大语言模型与语音识别 (STT) 服务商配置模板：

| 服务商 | 协议类型 | 默认对话模型 | 支持 STT 语音识别 |
|--------|---------|-------------|------------------|
| **DeepSeek** | OpenAI 兼容 | `deepseek-chat` / `deepseek-reasoner` | 搭配系统 STT |
| **OpenAI** | 官方/兼容 | `gpt-4o` / `gpt-4o-mini` | `whisper-1` |
| **Anthropic** | 官方 Messages 原生 | `claude-3-5-sonnet-latest` | 搭配系统 STT |
| **通义千问 (Qwen)** | DashScope 兼容 | `qwen-plus` / `qwen-max` | `qwen-audio-asr` |
| **智谱 GLM** | 官方 PAAS 兼容 | `glm-4-plus` / `glm-4-air` | 搭配系统 STT |
| **月之暗面 (Moonshot)** | OpenAI 兼容 | `moonshot-v1-8k` | 搭配系统 STT |
| **硅基流动 (SiliconFlow)** | OpenAI 兼容 | `deepseek-ai/DeepSeek-V3` | `FunAudioLLM/SenseVoiceSmall` |
| **Groq** | OpenAI 兼容 | `llama-3.3-70b-versatile` | `whisper-large-v3` |
| **xAI (Grok)** | OpenAI 兼容 | `grok-2` | 搭配系统 STT |
| **Google Gemini** | OpenAI 兼容 | `gemini-2.0-flash` | 搭配系统 STT |
| **字节豆包 (Doubao)** | 火山方舟兼容 | `doubao-1-5-pro-32k-250115` | 搭配系统 STT |
| **自定义 / Ollama** | 本地/私有网关 | 自定义模型名 | 自定义 STT |

---

## 环境要求

- **Python 3.10** 或更高版本（支持 `uv`、`pip` 或 Conda 虚拟环境）
- **GPT-SoVITS**（语音合成推理服务端，建议 v2 / v2ProPlus）
- **FFmpeg**（可选，用于 Telegram 语音消息 OGG/Opus 与 16kHz WAV 双向转码）

---

## 快速上手

### 1. 启动 GPT-SoVITS 后端

确保 GPT-SoVITS `api_v2.py` 已正常启动并监听在 `http://127.0.0.1:9880`：

```bash
# 进入 GPT-SoVITS 根目录
python api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
```

### 2. 启动 Galgame2Voice 伴侣服务

在项目根目录下直接运行 Windows 批处理脚本（自动检测 Python 环境与依赖）：

```bash
# 一键双击启动（自动检测并拉起 GPT-SoVITS，等待模型就绪，启动服务并自动打开浏览器）
# 退出时直接关闭控制台窗口或按 Ctrl+C，Windows 内核级 Job Object 自动联动终止后台进程并释放全部显存。
.\启动.bat
```

启动器会自动完成：GPT-SoVITS 引擎检测与后台拉起（就绪等待 + 日志落盘）、端口冲突自动降级、健康轮询、浏览器自动打开，以及退出时的自动资源回收。

或者使用 Python 原生命令行：

```bash
# 安装依赖（推荐 uv，或使用 pip install -e .）
uv sync

# 启动 FastAPI 服务
uvicorn galgame2voice.main:app --host 127.0.0.1 --port 8080 --reload
```

启动后即可通过浏览器访问：
- **聊天主界面**：[http://127.0.0.1:8080/](http://127.0.0.1:8080/)
- **管理控制台**：[http://127.0.0.1:8080/settings.html](http://127.0.0.1:8080/settings.html)
- **OpenAPI 接口文档**：默认关闭，需要时设置环境变量 `GALGAME2VOICE_ENABLE_DOCS=true` 开启

> 🔑 首次启动会自动生成控制台访问 Token 并打印在启动日志中（也可通过
> `GALGAME2VOICE_CONSOLE_TOKEN` 环境变量固定）。前端首次访问时输入一次即可。

---

## Telegram 机器人配置

1. 打开管理控制台 [http://127.0.0.1:8080/settings.html](http://127.0.0.1:8080/settings.html) ➔ **Telegram 设置**
2. 填入从 `@BotFather` 获取的 `Bot Token`
3. 配置 **管理员 ID 白名单**（`telegram_admin_ids` 或环境变量 `TELEGRAM_ADMIN_IDS`，逗号分隔）：未配置时任何人都可执行全局管理命令，强烈建议配置
4. 若在国内网络环境下，勾选 **启用 HTTP/SOCKS5 代理**（例如 `127.0.0.1:10809`）
5. 保存配置后服务自动启动长轮询，支持以下指令：
   - `/start` - 查看欢迎语并初始化角色
   - `/reset` - 清空当前上下文历史
   - `/voice` - 查看/切换音色配置
   - `/model` - 查看当前 LLM / STT 模型
   - `/console` - 在私聊中安全获取专属后台管理链接
   - 🎙️ **直接发送语音**：机器人自动转码识别、调用大模型并回复女主语音

---

## 运行自动化测试

项目内置完整的 Pytest 自动化测试套件（覆盖数据库持久化、模型适配器、分句算法、GPT-SoVITS 互斥客户端与 SSE 流式管道）：

```bash
python -m pytest -v
```

---

## 开源许可

本项目遵循 MIT License 开源。
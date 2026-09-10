# Galgame2Voice 🎮🎙️

> **工业级 Galgame 角色本地 AI 对话与实时语音流式交互引擎 (v2.0)**  
> 完美适配 GPT-SoVITS V2 Pro Plus，提供情绪感知自适应音色、现代化单页控制台 (SPA)、多会话隔离、双语流式推流、硬件精度自适应校准及 Telegram 伴侣机器人。

---

## 🌟 核心特性 (v2.0 Highlights)

- 🎭 **AI Dynamic Voice（情绪感知自适应音色与韵律）**：
  - **情感感知多维映射**：基于大模型输出的情感语义，自适应识别 7 种核心情绪（温柔 `gentle`、开朗 `happy`、悲伤 `sad`、傲娇 `tsundere`、生气 `angry`、害羞 `shy`、冷静 `cool`）。
  - **动态选取参考音频**：自动从角色包 `refs/` 中选取对应情绪的高保真参考音频，配合动态语速（`speed`: 0.8~1.4）与采样温度（`temperature`: 0.7~1.3），告别千篇一律的机械声线。
- 🧩 **角色包即插即用（Self-Contained Packages）**：
  - 核心系统与角色资产彻底解耦。将角色包放置于 `characters/` 目录下即可被引擎自动扫描、校验并加载，完全无需手动编写代码或重启服务。
  - 角色包与权重文件均处于本地沙箱环境，绝不上传云端，保障个人数字资产隐私。
- ⚡ **毫秒级双语流式推流与前瞻断句**：
  - 独创 `StreamingBilingualParser`，零阻塞并行提取大模型输出的中文心声与日文语音台词，前瞻式分句并管道化合成。
  - **双级 TTS 缓存**：L1 内存 LRU 缓存（`<0.005ms` 极速返回）+ L2 磁盘持久化 WAL 缓存，大幅降低重复对话时的 GPU 算力开销。
- 🎛️ **推理精度自适应校准与硬件运行模式**：
  - **FP16 半精度**：显存占用直降 50%，推理速度提升 1.5x~2x，RTX 20/30/40 等主流显卡首选。
  - **FP32 单精度**：高兼容性模式，彻底解决 GTX 16 系列（TU116/TU117）或部分显卡驱动下的静音/哑音故障。
  - **CPU 稳定模式 (免显存占用)**：专为入门/老旧显卡（如 MX450, GTX 1050/1650 显存 ≤ 4GB）设计。依托宿主机大内存（如 16GB）进行运算，彻底杜绝显存溢出 (CUDA OOM) 与显卡驱动崩溃。
  - **自适应试声探针 (Auto)**：服务启动后自动合成试声探测包并分析峰值振幅，静音时全自动平滑回退至 FP32，零学习成本。可通过 Web 控制台仪表盘一键无缝热切换（FP16 ⇄ FP32 ⇄ CPU）。
- 💻 **现代化单页控制台 (Unified SPA Web Console)**：
  - 基于极简高性能架构打造，首屏加载小于 50ms，彻底拔除历史多页面重定向与跳转延迟。
  - **双层设置架构**：
    - **全局设置（主界面左下角 ⚙️）**：模型提供商管理、TTS 质量预设、一键清空离线缓存、Telegram 机器人配置、系统状态诊断看板与 SoVITS 引擎热重启。
    - **会话设置（主界面右上角 🎚️）**：为当前会话独立绑定角色音色、专属 System Prompt、生成采样参数及 AI 自适应音色开关，实现多窗口与不同角色并行对话。
- ✈️ **Telegram 伴侣机器人（完全可选）**：
  - 默认彻底关闭，未启用时不启动后台轮询与网络握手，保证终端零冗余日志。
  - 开启后支持异步长轮询、多用户隔离、快捷打断、常用指令集（`/start`, `/reset`, `/voice`, `/settings`, `/status`, `/help`）以及专用的 HTTP/SOCKS5 代理热重载。
- 🛡️ **工业级安全脱敏与 16GB 内存保障**：
  - 全链路日志、异常回溯与 HTTP 响应体实施敏感信息过滤（API Key、Telegram Token、内嵌凭据 URL `user:password@` 100% 自动打码脱敏）。
  - 内置显存与内存水位防护，跨角色切换时触发主动垃圾回收与 PyTorch 缓存释放，16GB 内存设备稳定运行不崩溃。

---

## 🚀 快速开始 (Quickstart)

### 1. 环境准备
- **操作系统**：Windows 10/11 64位 或 Linux (Ubuntu 20.04+)
- **Python**：3.10 或更高版本
- **显卡配置**：推荐 NVIDIA 独立显卡（GTX 1060 以上，显存 4GB+）；亦支持 CPU 兼容模式。
- **语音引擎**：GPT-SoVITS V2 Pro Plus 官方整合包。

### 2. 运行项目

#### 方式一：Windows 一键快速启动（推荐）
直接双击运行项目根目录下的批处理脚本：
```cmd
启动.bat
```
如需显式指定推理硬件或精度启动：
```cmd
启动.bat --fp16          # 强制开启 FP16 半精度加速
启动.bat --fp32          # 强制使用 FP32 单精度兼容模式
启动.bat --cpu           # 强制使用纯 CPU 稳定模式 (免显存占用)
```

#### 方式二：命令行启动
```bash
# 1. 安装核心运行依赖
pip install -r requirements.txt

# 2. 启动服务（自动探测硬件与 GPT-SoVITS 引擎）
python scripts/run_server.py
```

常用命令行参数：
```bash
python scripts/run_server.py --help
  --host HOST             绑定监听地址 (默认: 127.0.0.1)
  --port PORT             监听端口 (默认: 8080，被占用时自动递增)
  --fp16                  启用 FP16 半精度推理 (显存省半，推理快)
  --fp32                  强制 FP32 单精度 (杜绝哑音静音)
  --cpu                   强制纯 CPU 稳定模式 (零显存占用，利用主机大内存)
  --precision {fp16,fp32,cpu,auto}
                          显式指定推理精度/运行模式
  --no-browser            启动后不自动唤起默认浏览器
  --check-only            仅执行环境依赖与硬件巡检，不启动常驻服务
```

---

## 🔧 环境变量速查表 (Environment Variables)

系统支持通过环境变量或根目录下的 `.env` 文件进行无缝配置：

| 环境变量名 | 默认值 | 类型 | 作用说明 |
| :--- | :--- | :---: | :--- |
| `HOST` | `127.0.0.1` | string | 后端 FastAPI 服务绑定的网卡监听地址 |
| `PORT` | `8080` | int | 后端服务监听端口（若被占用自动探测可用端口） |
| `LOG_LEVEL` | `INFO` | string | 日志记录级别 (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `GPT_SOVITS_BASE_URL` | `http://127.0.0.1:9880` | string | GPT-SoVITS 语音推理引擎的 HTTP API 地址 |
| `GPT_SOVITS_PRECISION` | `auto` | string | 强制推理精度与模式 (`fp16`, `fp32`, `cpu`, `auto`) |
| `TELEGRAM_ENABLED` | `false` | bool | 是否在启动时自动拉起 Telegram 伴侣机器人 |
| `TELEGRAM_TOKEN` | 空 | string | Telegram Bot API 访问凭据 Token |
| `TELEGRAM_PROXY` | 空 | string | Telegram 代理服务器地址（支持 `http://` 或 `socks5://`） |
| `AUDIO_RETENTION_MINUTES`| `30` | int | 合成临时音频在磁盘中的最长保留分钟数 |
| `GALGAME2VOICE_CONSOLE_TOKEN` | 空 | string | 设置控制台管理鉴权令牌（非空时访问设置需输入 Token） |
| `GALGAME2VOICE_ENABLE_DOCS` | `false`| bool | 是否开启 `/docs` 与 `/redoc` 接口文档页面 |
| `GALGAME2VOICE_SKIP_MEM_CHECK` | `false`| bool | 是否跳过跨角色切换时的空闲内存保护检查 |

---

## 🎭 角色包规范与扩展 (Character Packages)

本引擎采用**完全自包含（Self-Contained）**的角色包架构。将角色包解压至 `characters/` 目录下即可即插即用：

```text
characters/
└── 角色名/
    ├── manifest.json         # 角色配置清单（ID、名称、情绪映射、默认音色参数等）
    ├── system_prompt.txt     # 角色专属 System Prompt（人设、口吻、好感度引导）
    ├── gpt.ckpt              # GPT 权重模型二进制文件
    ├── sovits.pth            # SoVITS 权重模型二进制文件
    └── refs/                 # 情绪参考音频目录（3~10 秒高保真无损音频）
        ├── gentle.ogg
        ├── happy.ogg
        ├── angry.ogg
        ├── sad.ogg
        ├── shy.ogg
        ├── tsundere.ogg
        └── cool.ogg
```

> **资产隔离说明**：所有模型权重与角色目录均已被 `.gitignore` 严格隔离，不会意外提交至代码仓库。

---

## ✈️ Telegram 伴侣机器人指南

在主界面全局设置中开启 Telegram Bot 后，您可以通过手机或桌面客户端随时随地与角色聊天：

### 支持的指令集
- `/start`：唤出欢迎信息与快速交互键盘菜单
- `/reset`：清空当前 Telegram 会话的历史上下文与记忆碎片
- `/voice [名称]`：快速查询或切换当前绑定的角色音色
- `/status`：实时查看系统当前运行状态、CPU/内存/显存负载
- `/settings`：查看当前语音生成采样参数与 AI 动态音色状态
- `/help`：获取完整的操作指令与使用提示

### 代理连通性
若您处于需要代理访问 Telegram 的网络环境，直接在网页设置面板中填写 **Telegram 代理地址**（例如 `http://127.0.0.1:7890` 或 `socks5://127.0.0.1:10808`），点击「保存并热重载」即可立刻生效，无需重启后端服务。

---

## 🛡️ 安全、隐私与内存控制

- **零敏感信息泄漏**：全自动日志与报错脱敏引擎，覆盖 Bearer Token、OpenAI/DeepSeek Key、Telegram Token 及 URL 用户身份凭据。
- **16GB 主机内存防护**：在模型加载与音色切换前主动触发 `release_system_memory()`（包含 Python `gc.collect()` 与 PyTorch CUDA/MPS 显存池释放），并在切换完成后二次回收，杜绝长期运行内存缓慢膨胀。
- **完全本地化沙箱**：对话历史、记忆数据、好感度等级均储存在本地 SQLite 数据库中，自主掌控个人隐私。

---

## 📄 开源许可证

本项目采用 [MIT License](LICENSE) 开源。

# 星光咖啡馆与死神之蝶 · 四季夏目 聊天 AI

> 中文显示 + 日文语音的 Galgame 女主聊天机器人，支持网页与 Telegram 两种入口。

一个基于 Spring Boot 的 AI 聊天应用，集成大语言模型（DeepSeek 等 **OpenAI 兼容**接口，可换任意服务商）与 **GPT-SoVITS** 语音合成，扮演《星光咖啡馆与死神之蝶》（喫茶ステラと死神の蝶）中的角色「四季夏目」与玩家对话：

- 🗣️ 让大模型按 JSON 输出「中文台词 + 日文台词」
- 🀄 中文用于页面 / 聊天显示
- 🇯🇵 日文喂给 GPT-SoVITS 合成女主语音并播放
- 💬 网页聊天界面（`/`）与 Telegram 机器人两种入口
- 🎛️ 网页控制台（`/settings.html`）与 Telegram 命令，支持多用户独立配置

## 核心思路

GPT-SoVITS **不会翻译，只会照着文本念**。所以要做到"显示中文、口播日文"，链路是：

```
用户输入
   ↓
DeepSeek 一次性输出：
   ├─ chinese（中文台词）→ 显示在页面
   └─ japanese（日文台词）→ 喂给 GPT-SoVITS 合成语音 → 播放
```

因此最关键的一点是：**让大模型按 JSON 格式输出双语内容**（通过 `AiModelManager` 里的系统提示词实现）。

## 训练得到的两个模型文件

| 文件 | 作用 |
|------|------|
| `xxx.ckpt`（GPT 模型） | 音色克隆，决定"像不像女主" |
| `xxx.pth`（SoVITS 模型） | 语音合成，决定"清不清楚、自不自然" |

**两个必须同时使用，缺一不可。**

## 配置 API Key

接口走的是 **OpenAI 兼容格式**，所以 Key **不一定是 DeepSeek 的**——任何兼容服务商都能用，只需把 Key、地址、模型名换成对应的即可。

> 注意：**网页聊天**的 Key 在网页「AI 设置」里填（存浏览器，不读配置文件）；下面 `application.yaml` 里的配置**仅供 Telegram 机器人**使用。

先把 `application.example.yaml` 复制一份并重命名为 `application.yaml`，然后填入你的 Key：

```yaml
galgame:
  api-key: "sk-你的key"                       # 任意 OpenAI 兼容服务商的 Key
  api-base-url: "https://api.deepseek.com"     # 对应服务商的地址
  chat-model: "deepseek-v4-flash"              # 对应服务商的模型名
  telegram-bot-token: ""                       # 需要 Telegram 机器人时填写，留空则禁用
```

常用服务商示例：

| 服务商 | `api-base-url` | 模型示例 |
|--------|----------------|----------|
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` |
| 月之暗面 Moonshot | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |

> 也可以完全不写 Key：网页聊天在「AI 设置」里填、Telegram 用 `/setkey` 填。

## 使用步骤

### 1. 启动 GPT-SoVITS 的 API

1. 把 `.ckpt` 放进 GPT-SoVITS 项目的 `GPT_weights_v2/` 文件夹
2. 把 `.pth` 放进 `SoVITS_weights_v2/` 文件夹
3. 启动接口：
   ```bash
   python api_v2.py
   ```
   默认监听 `http://127.0.0.1:9880`

### 2. 准备女主参考音频

准备一段女主**干净、无杂音**的几秒原声，以及它对应的文字内容。

### 3. 修改配置

#### 3.1 编辑 `src/main/resources/application.yaml` 中的 `gpt-sovits` 配置：

```yaml
gpt-sovits:
  base-url: http://127.0.0.1:9880
  ref-audio-path: 参考音频路径.wav   # 相对 GPT-SoVITS 项目根目录
  prompt-text: 参考音频对应的文本
  prompt-lang: ja
  text-lang: ja
```

#### 3.2 配置合成模型（重要）

新版 api_v2.py 的 `/tts` 接口**不再通过请求体传模型权重**，而是从
`GPT_SoVITS/configs/tts_infer.yaml` 的 `custom` 段加载模型。请把该文件
`custom` 段里的 `t2s_weights_path`（GPT 模型 .ckpt）和 `vits_weights_path`
（SoVITS 模型 .pth）改成你训练好的女主模型：

```yaml
custom:
  t2s_weights_path: GPT_weights_v2/siki2-e50.ckpt
  version: v2
  vits_weights_path: SoVITS_weights_v2/siki_e20_s10280.pth
```

> 注意：`ref-audio-path` 和各权重路径都是相对 GPT-SoVITS 项目根目录的路径，且这些文件要放在 GPT-SoVITS 服务器上。

### 4. 启动后端

```bash
./mvnw spring-boot:run
```

### 5. 打开聊天页面

浏览器访问：`http://localhost:8080/`

输入中文，页面会显示中文回复，同时自动播放女主日文语音。

## Telegram 机器人（可选）

在 `application.yaml` 里填写 `telegram-bot-token` 后启动，Bot 会自动注册（走 `galgame.telegram-proxy-*` 代理）。支持：

| 命令 | 说明 |
|------|------|
| `/model` / `/voice` | 查看当前模型 / 语音设置 |
| `/setkey` `/seturl` `/setmodel` `/setsttmodel` | 设置 API Key / 地址 / 对话模型 / 语音识别模型 |
| `/setbatch` `/speed` `/temp` `/settopk` `/settopp` `/setseed` `/setsplit` | 设置 TTS 参数 |
| `/console` | 获取你的专属网页控制台链接 |
| `/help` | 帮助 |

支持直接发文字或语音（语音先经 STT 转文字再对话），回复为文本 + 日文语音条。

> Telegram API 在国内需代理，默认走 `127.0.0.1:10809`，可在 `application.yaml` 的 `galgame.telegram-proxy-*` 修改。

## 网页控制台

启动后访问 `http://localhost:8080/settings.html`，输入 Telegram 里 `/console` 获得的 token，即可在网页上管理自己的 API / TTS 配置。

## API 说明

### GET /ai/chat

参数：`prompt`（用户输入）

返回 JSON：
```json
{
  "chinese": "显示的中文台词",
  "japanese": "日文台词",
  "audioUrl": "/audio/tts_xxx.wav"
}
```

## 项目结构

```
src/main/java/org/example/springai/
├── SpringaiApplication.java        # 启动类
├── config/
│   ├── AudioCleanupTask.java       # 定时清理过期音频
│   ├── ConfigStore.java            # 多租户配置存储（configs/<chatId>.json）
│   ├── GalgameConfig.java          # 全局默认配置（持久化 galgame-config.json）
│   ├── GptSovitsProperties.java    # GPT-SoVITS 配置
│   └── WebConfig.java              # /audio/** 静态资源映射
├── controller/
│   ├── AiController.java           # 网页聊天接口
│   └── ConfigController.java       # 控制台配置读写接口
├── service/
│   ├── AiModelManager.java         # DeepSeek 客户端 + 角色系统提示词
│   ├── ChatService.java            # 解析双语 JSON
│   ├── GptSovitsService.java       # 调用 GPT-SoVITS /tts
│   └── TtsOptions.java             # TTS 参数
└── telegram/
    ├── TelegramBotConfig.java      # Bot 注册与代理配置
    └── TelegramGalBot.java         # Telegram 消息/语音处理
src/main/resources/
├── application.example.yaml        # 配置模板（复制为 application.yaml）
├── nat002_077.ogg                  # 参考音频
└── static/
    ├── index.html                  # 聊天页面
    └── settings.html               # 网页控制台
```

## 技术栈

- Spring Boot 4.0.7 / Java 17
- Spring AI 2.0.0（DeepSeek）
- GPT-SoVITS（api_v2.py 语音合成）
- Telegram Bots（telegrambots 6.9.7.1）
- Jackson 2.17 / Lombok

## 致谢

本项目使用了以下开源项目，特此致谢：

| 项目 | 用途 | 链接 |
|------|------|------|
| GPT-SoVITS | 女主语音合成（音色克隆 + TTS） | <https://github.com/RVC-Boss/GPT-SoVITS> |
| Spring Boot | 应用框架 | <https://spring.io/projects/spring-boot> |
| Spring AI | LLM 接入 | <https://spring.io/projects/spring-ai> |
| Telegram Bots | Telegram 机器人 SDK | <https://github.com/rubenlagus/TelegramBots> |

> 语音合成部分依赖 GPT-SoVITS 的 `api_v2.py` 接口，使用前请先启动 GPT-SoVITS 服务（见上文「使用步骤」）。GPT-SoVITS 的模型与代码版权归其原作者（RVC-Boss）所有。
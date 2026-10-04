# 安全说明（SECURITY.md）

本文档描述 galgame2voice 当前实际实现的安全机制、部署边界与已知限制。

## 部署边界

本项目按**单机单用户个人工具**设计，默认只监听 `127.0.0.1`。如需暴露到局域网或公网，
请自行叠加反向代理、TLS 与更强的访问控制——当前的安全机制以本机使用为威胁模型。

## 认证

- 除 `/api/health`、`/status`、静态资源与音频文件外，**所有 API 路由均声明了控制台 Token 认证依赖**
  （`Authorization: Bearer <token>` 或 `X-Console-Token` 头，见 `main.py` 的 `auth_deps`）。
- **本地默认模式下鉴权实际上是关闭的**：`Settings.auth_disabled` 默认 `True`
  （`galgame2voice/config.py`），`require_auth()` 在该开关为真时直接放行
  （`galgame2voice/security/auth.py`）。容器/局域网/公网部署必须显式设置
  `GALGAME2VOICE_AUTH_DISABLED=0`（`docker-compose.yml` 已内置该设置）。
- Token 来源优先级：环境变量 `GALGAME2VOICE_CONSOLE_TOKEN` > SQLite `settings.console_token`。
- 首次启动时若 DB 中无 Token，会自动生成 `uuid4().hex`：加密存入 SQLite，明文写入 `data/.console_token`（0600）；启动日志仅打印脱敏值（前后各 4 位），不会输出完整 token。
- 比较使用 `hmac.compare_digest`（常量时间）。
- 前端（聊天页与设置控制台）在收到 401 时会弹出输入框收集 Token 并存入 `sessionStorage`（不写
  `localStorage`、不落盘，关闭标签页即失效）后自动重试。
- `/docs` 与 `/redoc` 默认关闭，需 `GALGAME2VOICE_ENABLE_DOCS=true` 显式开启。

## SSRF 防护（LLM 服务商接口）

- 所有用户提供的 LLM provider `api_base_url` 均经过 `security/url_guard.py` 校验：
  - 仅允许 http/https；官方预置服务商域名强制 https；
  - DNS 解析后拒绝环回/私网/链路本地/保留/多播网段（含云元数据 `169.254.169.254`）。
- 连接本地模型（Ollama/vLLM 等）需显式开启 `allow_private_llm_endpoints`。Web 控制台没有该开关，
  只能写入 SQLite 设置行：`POST /api/config`，请求体 `{"allow_private_llm_endpoints": true}`。
- **不校验 GPT-SoVITS 地址**：其默认值 `http://127.0.0.1:9880` 本身就是合法私网端点。
- 已知限制：采用"保存时预检"而非传输层逐跳校验，DNS rebinding 理论上仍可行（预检通过后
  域名重解析到私网）；该残余风险在认证门禁之后，且要求操作者主动配置恶意域名。

## Telegram 机器人

- **管理员白名单**：`settings.telegram_admin_ids`（逗号分隔）或环境变量 `TELEGRAM_ADMIN_IDS`。
  配置后，全局管理类操作（切换模型/音色、修改全局推理参数、清空缓存）仅限白名单用户；
  **白名单为空时 fail-closed**：所有用户（包括管理员自己）的文字与语音消息都会被拒绝，
  管理类按钮回调同样被拒，启动日志会给出警告。
- **群聊隔离**：群聊会话键为 `tg_{chat_id}_{user_id}`，每个成员拥有独立的对话历史、
  长期记忆、昵称与好感度；私聊保持旧键 `tg_{chat_id}` 以兼容既有历史。

## 输入防护

- 提示词上限 4000 字符，会话 ID 上限 128 字符，TTS 文本上限 2000 字符。
- TTS 选项（speed/top_k/top_p/temperature/batch_size/fragment_interval/seed）在 API 边界
  校验数值范围与字符串长度，内部再统一 clamp 兜底。
- 请求限流：滑动窗口（全局 240 次/分、单 IP 120 次/分、聊天/合成类 30 次/分），
  超限返回 429；测试可用 `GALGAME2VOICE_RATE_LIMIT_DISABLED=1` 关闭。

## 数据与凭据

- **API Key、Telegram Token、控制台 Token 在写入 SQLite（`data/galgame2voice.db`）前已加密**：
  Windows 使用 DPAPI（`dpapi:` 前缀），其他平台回退到 AES-GCM / HMAC 认证的加密流（`enc:` 前缀），
  密钥来自 `GALGAME2VOICE_SECRET_KEY`（或 `GALGAME2VOICE_MASTER_KEY`）环境变量，缺省时使用
  `data/.master_key`（0600 权限）。读写分别经 `security/crypto.py` 的 `encrypt_secret()` /
  `decrypt_secret()`，在 `database/crud_modules/settings_cache.py`、`providers.py` 落库前调用，
  历史明文记录由 `database/migrations.py` 自动改写。仍需依赖文件系统权限保护密钥文件与 DB。
- 所有 API 响应对密钥做脱敏（`sk-****xxxx` 形式）；自定义认证头同样脱敏，
  且前端回传的脱敏值不会被写回覆盖真实值。
- 用户生成的音频文件以 `private, max-age=0` 缓存策略返回，不进共享代理缓存。

## 已知边界（有意为之的简化）

- 单用户模型：session_id / user_id 由客户端声明，认证即门禁，不做多租户所有权校验。
- CRUD 层多个写操作各自独立提交，未做全面事务上移（关键路径——好感度累计与记忆
  upsert——已原子化）。
- 前端源码已按模块拆分（`frontend/src/*.js` + `src/controllers/`），但依赖 Vite 构建：
  `npm run build` + `node deploy.mjs` 才产出 `galgame2voice/static/`，仓库内同时保留了已构建产物。

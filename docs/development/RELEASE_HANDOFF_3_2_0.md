# v3.2.0 正式发布交接

本次发布汇总 v3.1.2 之后的全部功能演进与质量修复：新增「导入角色包」（支持选择文件夹、`.zip` 或拖入窗口）与「兑换码」查询下载角色包；下线 Telegram 机器人及相关设置页，移除旧同步对话及未使用的遗留 HTTP 接口；应用内「检查更新」改为仅查询 GitHub Releases 并提供下载入口；统一界面文案与诊断提示，并补齐历史未分句回复语音重生成、多标签页 409 同步及自定义运行目录等修复。数据库 schema 版本保持为 `7`，Python 与前端依赖保持锁定不升级。历史节点记录见 [v3.1.2 正式发布交接](RELEASE_HANDOFF_3_1_2.md)。

## 本次重点核验范围

- **导入角色包（`设置中心 → 角色与会话 → 导入角色包`）**：
  - 选择角色包文件夹、`.zip` 压缩包或直接拖入设置窗口后，后端校验 `manifest.json`、权重及参考音频并落入 `characters/`，前端自动刷新列表并选中新角色，无需手动复制或重启。
  - 不合格压缩包、目录少进一层或损坏清单能给出中文原因并拒绝导入，不影响已有角色包；本机已有同名角色时经二次确认可正常覆盖。
- **兑换码下载角色包（`设置中心 → 角色与会话 → 兑换码`）**：
  - 粘贴以 `G2V1.` 开头的兑换码并点击「查询」后列出可下载角色包，点击「下载并导入」实时显示进度（跨分区不支持硬链接时自动回退复制），完成后自动导入并选中该角色。
  - 查询失败时兑换码保留在输入框中便于核对重试，查询成功后清空输入框，且兑换码不会写入本机数据库、`data/` 或浏览器本地存储；无可下载包时给出说明；下载结束或取消同名覆盖后进度文字消失，确认覆盖同名角色时直接复用已下载包而不重复下载。
- **从 v3.1.2 升级后旧的 Telegram 设置兼容**：
  - Telegram 机器人运行时、测试接口及设置中心「Telegram」页已移除，SQLite 数据库 schema 版本仍为 `7`。
  - 直接迁移 v3.1.2 的 `data/galgame2voice.db` 时，旧 `settings` 表中的 `telegram_*` 列与字段值原样保留，启动与读写全局配置不报错；旧目录迁移来的 `.env` 若仍含 `TELEGRAM_*` 变量会被安全忽略（`extra="ignore"`），不阻断启动；`galgame2voice doctor` 不再检查 Telegram 配置。
  - 旧库中的 `tg_*` / `channel="telegram"` 历史会话及含 Telegram 会话的旧版聊天备份 JSON，仍可在网页端正常打开回看、继续对话、重新生成语音以及导出与导入。
- **浏览器里已有的聊天记录与升级兼容**：
  - 同源地址与端口下，浏览器原有的 `localStorage` 会话缓存与 IndexedDB 语音缓存（自动按最近 100 条滚动保留，用户录音不清理）继续可读；跨目录或更换端口前，仍通过「备份与更新 → 导出完整聊天备份」迁移。
  - 早期版本留下的未分句历史回复（`lines_json IS NULL`）或整段格式回复，在旧语音过期或丢失后可在 LOG 中用当前音色重新生成并持久化；默认旧欢迎语加载时自动刷新为现行说明，真实对话消息与时间戳保持不变。
  - 会话设置已移除「回复格式」与旧「日文原文显示」无效项，浏览器与服务端中的存量会话配置、长会话未回复提问重试状态及含 `system` 记录的历史备份均可兼容加载（历史备份中的 `system` 记录不再拼入后续模型指令）。

## 发布流程

1. 合并前在最新 `main` 基线上执行 `npm --prefix frontend run deploy` 同步 `galgame2voice/static/`，等待 PR 全部 CI 门禁（`lint-and-test` Python 3.10/3.12、`test-linux`、`dependency-audit`、`frontend-build`、`docker-build`）通过后，按已核对的 HEAD squash 合并至 `main`。
2. 在合并后的 `main` 提交上创建并推送标签：
   ```bash
   git tag v3.2.0 <main合并提交SHA> && git push origin v3.2.0
   ```
   确认自动触发 `.github/workflows/release.yml`；若未自动触发，核对无在途运行后可在 GitHub Actions 对 `v3.2.0` 标签手动触发一次 `workflow_dispatch`。
3. 等待 Release 工作流中 `build-release`（Ubuntu：版本一致性门禁 `verify_release_version.py --release-tag v3.2.0`、Ruff、全量 pytest、前端测试与构建、源码包 `package_release.py`）与 `build-windows-portable`（Windows：版本一致性门禁、便携冒烟单元测试、前端构建、PyInstaller 便携包构建、`verify_windows_portable.py`、`smoke_windows_portable.py` 及 `smoke_windows_turn_reply.py --with-browser-ui`）全部成功。
4. **手动创建并发布正式 GitHub Release**：`release.yml` 仅具 `contents: read` 权限，只上传 GitHub Actions 工作流构件（`galgame2voice-release` 与 `galgame2voice-windows-x64`），**不会自动创建草稿或正式 Release**。维护者需从该次成功工作流下载以下 4 个产物并核对 SHA-256，随后在 `v3.2.0` 标签上手工创建正式 GitHub Release 并上传附件（不使用本地历史构建替代工作流产物）：
   - `galgame2voice-v3.2.0.zip` 与 `galgame2voice-v3.2.0.zip.sha256`
   - `galgame2voice-v3.2.0-windows-x64.zip` 与 `galgame2voice-v3.2.0-windows-x64.zip.sha256`
5. Release 说明填入 `CHANGELOG.md` 中 `3.2.0` 小节摘要、升级数据迁移提示及 CI / Release 工作流链接；不向已打标签的提交追加“记录 CI 结果”提交。

## 验收清单

本地自动检查命令（从仓库根目录执行）：

```powershell
uv run python scripts/verify_release_version.py --release-tag v3.2.0
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run deploy
uv run --frozen --extra dev ruff check galgame2voice scripts
uv run --frozen --extra dev pytest -q
uv run --frozen --extra desktop python scripts/package_release.py
uv run --frozen --extra desktop python scripts/build_windows_portable.py
uv run --frozen --extra desktop python scripts/verify_windows_portable.py dist/galgame2voice-v3.2.0-windows-x64.zip
uv run --frozen --extra desktop python scripts/smoke_windows_portable.py dist/galgame2voice-v3.2.0-windows-x64.zip
$expectedSha = (Get-Content dist/galgame2voice-v3.2.0-windows-x64.zip.sha256 -Raw).Trim().Split()[0]
uv run --frozen --extra desktop python scripts/smoke_windows_turn_reply.py --archive dist/galgame2voice-v3.2.0-windows-x64.zip --expected-sha $expectedSha --scratch .local/smoke-turn-reply --with-browser-ui
```

完整浏览器交互场景与记录要求见 [RELEASE_ACCEPTANCE.md](RELEASE_ACCEPTANCE.md)，发布前额外手工确认：

- [ ] 解压 `galgame2voice-v3.2.0-windows-x64.zip` 至全新可写目录启动，通过文件夹选择、`.zip` 选择及窗口拖拽分别导入测试角色包，确认角色自动选中、立绘与情绪参考音频正常可用。
- [ ] 使用真实可用的 `G2V1.` 测试兑换码验证查询、下载进度、自动导入选中及同名角色二次确认覆盖（不重复下载），确认数据库与浏览器存储未留存兑换码原文。
- [ ] 复制一份含 Telegram 配置与 `tg_*` 历史会话的 v3.1.2 `data/`（及含 `TELEGRAM_*` 的测试 `.env`）到新便携目录启动，确认无报错、设置中心无 Telegram 页、原会话在网页与备份导出导入中完整可读。
- [ ] 在已有历史会话的浏览器中打开新版本，确认旧会话列表摘要、LOG 回看、过期单句语音重生成及完整备份导出均正常。

## 使用、升级与验证边界

- Windows 普通用户下载 `galgame2voice-v3.2.0-windows-x64.zip`，完整解压至可写目录后双击 `启动.bat` 或 `Galgame2Voice.exe`；包内自带 Python 运行时、FFmpeg 及空 `data`、`audio`、`logs`、`characters`、`backgrounds` 目录，模型 API 凭据、GPT-SoVITS 引擎与授权角色素材由用户自行准备。
- 从 v3.1.2 升级时先关闭旧程序，将新版解压到新目录，再复制旧目录的 `data`、`audio`、`characters`、`backgrounds`（及个人 `.env`）；同机同 Windows 账户下 DPAPI 加密凭据可直接沿用，跨机器或跨系统账户需重新填写 API Key，详见 [Windows 便携版使用说明](../WINDOWS_PORTABLE.md)。
- 自动化冒烟与浏览器 UI 验收采用合成 LLM/TTS 替身桩（`scripts/dev_standins.py`）与合成测试资源，能验证打包产物、流式解析、单句恢复、存储迁移与界面交互契约，但不能替代真实云端模型计费鉴权、真实兑换码分发服务连通性、真实 GPU + GPT-SoVITS 推理显存/精度表现及第三方角色立绘视觉对齐的人工核验。

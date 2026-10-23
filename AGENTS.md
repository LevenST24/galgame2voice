# AGENTS.md

本文件为参与本仓库开发与维护的 AI Agent 提供核心指引。开工前请通读本规范。

---

## 项目概览

- **后端**：位于 `galgame2voice/`，基于 FastAPI + SQLite (WAL 模式)，提供 REST API、SSE 聊天流与逐句 TTS 调度。
- **前端**：位于 `frontend/`，纯原生 ES 模块（无前端框架）+ Vite 构建。
- **构建产物**：编译部署产物直接提交在 `galgame2voice/static/`，后端由 FastAPI 静态托管。

---

## 常用命令（以 `.github/workflows/ci.yml` 为准）

开发与验证请使用以下标准命令：

```bash
# 后端依赖安装
uv sync --frozen --extra dev

# 后端代码检查与格式化
uv run ruff check galgame2voice scripts

# 后端单元与集成测试
uv run pytest -q

# 前端依赖安装与测试
cd frontend && npm ci && npm test

# 前端构建与静态产物同步部署（CI 会重新构建并与 galgame2voice/static/ 比对）
cd frontend && npm run deploy
```

---

## 核心开发规则

1. **游戏素材与模型仅保存在本地**：
   - `characters/`、`backgrounds/`、`skins/`、模型权重文件（`.ckpt`、`.pth`）以及音频文件绝不提交进 Git 仓库。
   - 测试环境使用 `tests/conftest.py` 与 `scripts/dev_standins.py` 提供的替身桩服务，不需要真实 GPU 或 GPT-SoVITS 实例。

2. **单一职责 PR 与分支管理**：
   - 一个 PR 只做一件事。
   - 每次均从最新的 `main` 分支新建特性分支，不要叠 PR（仓库合并策略为 squash 合并）。

3. **前端静态产物冲突解决**：
   - 每个前端 PR 在 `npm run deploy` 时都会重新生成带内容哈希的静态文件，两个前端 PR 必定会在 `galgame2voice/static/` 产生冲突。
   - 遇到冲突时**禁止手动合并**该目录：先取 `main` 分支的对应目录版本，然后在 `frontend/` 重新执行 `npm run deploy` 提交重新生成的文件。

4. **严谨的修复与测试规范**：
   - 每个 Bug 修复必须明确阐明：“在什么输入、什么状态下出什么错”。
   - 必须附带一个在修复前必定会重现失败、修复后通过的测试用例。

5. **克制设计原则**：
   - 不加界面上用户无法配置或感知不到的冗余选项。
   - 不为现实中不可能发生的分支情况写过度防御代码。
   - PR 描述中不写未经自动化测试或真实验证证明的效果。

6. **新增会话设置项同步三处白名单**：
   - 若新增会话级设置（Session Settings），必须同时添加到以下三处白名单：
     1. `frontend/src/store.js` 中的 `DEFAULT_SESSION_SETTINGS`
     2. `frontend/src/session_data.js` 中的 `pickSettings`
     3. `galgame2voice/services/chat_backup.py` 中的 `SESSION_SETTINGS`

7. **数据库变更策略**：
   - 数据库迁移遵循“能只加列就只加列”的原则，使用 `galgame2voice/database/migrations.py` 中的 `_add_column_if_missing` 增量补列。
   - 不要轻易提升数据库 schema 版本号（`CURRENT_SCHEMA_VERSION`）：版本号一旦提升，旧版客户端与程序将无法读取新库文件。

8. **更新日志与界面语言**：
   - 用户可感知的改动均需记录进 `CHANGELOG.md` 的 `[Unreleased]` 小节。
   - 界面面向用户的文案统一使用中文。

9. **浏览器环境时序注意事项**：
   - `SpeechRecognition`（浏览器语音识别）通常在调用 `stop()` 之后才触发 `onresult` 送达最终识别文本。
   - `AudioContext.resume()` 在缺乏用户直接手势（User Gesture）交互时可能会处于 `suspended` 挂起状态，禁止无超时保护直接 `await` 它。

10. **代码提交规范**：
    - 提交信息必须使用英文 Conventional Commits 格式（例如：`fix(adv): ...`、`feat(api): ...`、`docs: ...`）。
    - 严禁在提交信息中添加 `Co-Authored-By`、`Generated with ...` 等自动化署名后缀。

---

## 相关重要文档

- **ADV 界面规格与设计契约**：[`docs/design/adv-ui.md`](docs/design/adv-ui.md)
- **测试架构与 4 层黑盒策略**：[`docs/development/TEST_INFRA.md`](docs/development/TEST_INFRA.md)
- **版本更新记录**：[`CHANGELOG.md`](CHANGELOG.md)

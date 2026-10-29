# Windows 发布验收

目标用户是未安装 Python、Node.js、FFmpeg 或 Docker 的 Windows 普通用户。
每次验收应使用最终 ZIP 的独立解压目录；只使用测试聊天、测试凭据和合成角色资源。
不要将开发目录中的 `data`、`audio`、`characters` 或 `.env` 复制进验收安装。
在临时安装中指定专用的测试引擎目录，避免自动发现开发机现有引擎和模型。

## 自动检查

从项目根目录执行，版本号按本次发布替换：

```powershell
npm --prefix frontend test
npm --prefix frontend run deploy
uv run --frozen --extra dev pytest -q
uv run --frozen --extra dev ruff check galgame2voice scripts
uv run --frozen --extra desktop python scripts/build_windows_portable.py
uv run --frozen --extra desktop python scripts/verify_windows_portable.py dist/galgame2voice-v3.1.0-windows-x64.zip
uv run --frozen --extra desktop python scripts/smoke_windows_portable.py dist/galgame2voice-v3.1.0-windows-x64.zip
```

`verify_windows_portable.py` 检查校验和、必需运行文件、空用户目录、模型和凭据排除，以及 ZIP、已部署网页和 Vite 产物的一致性。
它不执行程序；启动、音频转换、更新和数据库恢复由 smoke 脚本检查。

## 浏览器交互

浏览器验收必须连接从该 ZIP 解压并运行的程序。只使用 HTTP 静态页面或替代后端的页面测试，不能证明打包后端正常。

| 场景 | 通过条件 |
| --- | --- |
| 首次启动 | 完整解压后可打开网页，数据写在安装目录的 `data` 中；缺少语音引擎时有配置入口。 |
| 配置模型 | 密钥不回显；公网模型正常保存；本地模型在明确开启私网许可后可测试和聊天。 |
| 撤销私网许可 | 保留已保存配置；后续本地聊天、连接测试和模型列表请求均受到限制。 |
| 聊天与重启 | 台词逐句推进，播完后文本框回到玩家输入；刷新或重启程序后，会话设置和已完成的台词仍可在 LOG 中读取。 |
| 首页 | 重启后首页显示上次会话的最后一句；点「接着聊」进入对话后可以直接输入；「另开一段对话」能选角色、地点和时间。 |
| 角色与立绘 | 绑定两个测试角色后分别切换会话；名字牌、立绘、头像和人设保持对应。无立绘的角色清除旧图。 |
| 模型或引擎离线 | 用户能看到具体的配置或重试入口；收到的文字仍保留，恢复后可再次操作。 |
| 导出与导入 | 实际下载完整备份，检查消息内容和数量；导入新增会话，原会话仍在；刷新后导入结果仍在。 |
| 本机服务中断 | 保存、导出或删除失败时保留内容；服务恢复后能重试。 |
| 小窗口 | 设置可滚动、按钮可操作；立绘和快捷菜单不遮住文本框；竖向窗口使用 16:9 舞台并能输入和推进。 |

浏览器可单独通过应用内 Browser 验证；在这种情况下 smoke 可以使用 `--skip-browser`，但应在记录中注明同一 ZIP 的浏览器验收结果。

## 记录边界

- 保存最终 ZIP 的字节数、SHA-256、完整测试收尾行、验收截图和失败诊断。
- 使用合成 LLM/TTS 上游，能检查打包程序的请求、流式处理、存储和故障反馈，不能证明真实模型质量、显存占用或人脸部件的对齐。
- 实际语音引擎和真实角色资源需要单独验收。未执行的项目应注明未验证。
- 构建成功、API 返回 200 或浏览器启动成功，都不能替代完整流程验收。

# v3.1.2 正式发布交接

本次交付包含 v3.1.1 代码节点的全部改进，以及 Windows 便携版浏览器验收的初始导航修复。v3.1.1 标签保留，其 Release 工作流两次因 CDP 上下文丢失失败，未发布正式附件；不改写该标签，正式附件使用 v3.1.2。

## 修复范围

- 仅在浏览器就绪阶段确认受控目标页面的 URL 与加载状态；初始导航中的临时上下文丢失允许在既定时限内重试只读检查。
- 完整业务脚本在目标页面就绪后执行一次。业务阶段出现上下文丢失或其他错误仍然失败，避免重放设置、导出等操作。
- 保留原有设置窗口、LOG、修复音频和导出断言，保留浏览器与应用进程清理、失败截图及日志。
- Python、前端与锁文件只同步项目版本，不更换依赖。数据库 schema 仍为 7。

代码入口：`scripts/smoke_windows_ui.py`；协议边界回归：`tests/test_smoke_windows_ui.py`。既有轮次/音色架构及迁移边界见 [v3.1.1 历史交接](RELEASE_HANDOFF_3_1_1.md)。

本地增量证据：原函数在私有协议装置下可复现相同的初始上下文销毁错误；修复后独立执行驾驶器与界面组合测试，20 项通过、3 个子用例通过。独立脚本入口的 15 项 unittest（含真实 Chrome 的合成本机页面导航）通过；Ruff 与离线锁文件检查通过。正式测试不读取旧 Git 历史，也不会向后续用例泄漏 mock。

## 发布流程

1. PR 的六项 CI 和实际 Python/npm 审计全部通过后，按已核对的 HEAD squash 合并。
2. 在合并提交上创建新标签 `v3.1.2`。确认该标签的 Release 工作流运行；没有自动生成运行时，可核对不存在后手动在同一标签触发一次。
3. 等待源码测试/打包、Windows ZIP verify、真实 EXE smoke 和同一 ZIP 的 Chrome 业务验收全部成功。
4. 从成功的该次工作流下载源码与 Windows 产物，发布两个 ZIP 及标准构建生成的两个 `.sha256` 文件。历史本地 ZIP 不替代正式工作流产物。
5. Release 说明记录 PR CI、合并后 CI 和发布工作流的实际链接、结果及下载方式；不向已验证的代码追加“记录 CI 结果”的提交。

本地基础命令从仓库根目录执行：

```powershell
uv run --frozen --extra dev ruff check galgame2voice scripts
uv run --frozen --extra dev pytest -q tests/test_smoke_windows_ui.py
uv run --frozen --extra desktop python scripts/build_windows_portable.py
uv run --frozen --extra desktop python scripts/verify_windows_portable.py dist/galgame2voice-v3.1.2-windows-x64.zip
uv run --frozen --extra desktop python scripts/smoke_windows_portable.py dist/galgame2voice-v3.1.2-windows-x64.zip
```

实际完整门禁与浏览器参数见 [RELEASE_ACCEPTANCE.md](RELEASE_ACCEPTANCE.md)。

## 使用、升级与验证边界

- Windows 普通用户下载带 `windows-x64` 的 ZIP，完整解压后双击启动；包内有 Python 和 FFmpeg，模型服务、GPT-SoVITS 引擎及有权使用的角色资源由用户另行配置。
- 关闭旧安装后再备份与迁移 `data`、`audio`、`characters`、`backgrounds` 及自行创建的 `.env`。跨账户或机器时 Windows DPAPI 凭据可能需要重填；详见 [Windows 使用说明](../WINDOWS_PORTABLE.md)。
- 自动验收使用合成上游和测试资源，不证明真实商业服务可用性、GPU 推理质量、Telegram 公网通信、第三方立绘对齐或实际听感。
- PR #284/#287 的旧格式语音恢复与跨标签页修复同步仍未包含，后续按 [TODO.md](TODO.md) 的优先级与验收标准处理。

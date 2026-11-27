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
uv run --frozen --extra desktop python scripts/verify_windows_portable.py dist/galgame2voice-v3.2.13-windows-x64.zip
uv run --frozen --extra desktop python scripts/smoke_windows_portable.py dist/galgame2voice-v3.2.13-windows-x64.zip
uv run --frozen --extra desktop python scripts/smoke_windows_turn_reply.py --archive dist/galgame2voice-v3.2.13-windows-x64.zip --expected-sha <sha256> --scratch .local/smoke-turn-reply --with-browser-ui
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

浏览器验收可由 smoke 脚本传入 `--with-browser-ui` 在受控重启后对真实运行程序全自动执行（验证设置中心单对话框与 Tab 切换、LOG 历史与修复音频链接、界面导出备份）；亦可单独通过应用内 Browser 手动验证。在这种情况下 smoke 可以使用 `--skip-browser`，但应在记录中注明同一 ZIP 的浏览器验收结果。

## 记录边界

- 保存 ZIP 字节数、测试结果、截图与诊断；保留标准构建自动生成的校验文件，无需另行计算。
- 使用合成 LLM/TTS 上游，能检查打包程序的请求、流式处理、存储和故障反馈，不能证明真实模型质量、显存占用或人脸部件的对齐。
- 实际语音引擎和真实角色资源需要单独验收。未执行的项目应注明未验证。
- 构建成功、API 返回 200 或浏览器启动成功，都不能替代完整流程验收。

## PowerShell 验收调用示例

- `smoke_windows_portable.py` 会在中文、空格路径解压真实产物，隐藏系统 Python/FFmpeg，检查网页、文件选择器运行库、音频转换和凭据保存；未检测到可用浏览器时会自动跳过无头首页检查，**跳过不代表 UI 交互验收通过**。
- `smoke_windows_turn_reply.py` 会在临时目录真实解压便携 EXE，接入合成替身桩（`scripts/dev_standins.py`），检查“收到首句台词后关闭流 → 按真实用户消息 ID 解析助手 ID → 唯一单句修复 POST 持久化 → 同目录冷重启服务 → 历史分页/音频文件/WAV 格式与只读 SQLite 一致性”。

在 Windows 维护者环境中执行以下脚本，自动完成动态发现、校验核对与独立沙箱验收：

```powershell
# 1. 动态定位唯一发布包并读取同源 SHA-256 校验和
$archives = Get-ChildItem -Path "dist/*-windows-x64.zip" -File
if ($archives.Count -ne 1) {
    throw "预期 dist/ 下存在唯一的 Windows 发布包，实际找到 $($archives.Count) 个。"
}
$archivePath = $archives[0].FullName
$shaFile = "$archivePath.sha256"
if (-not (Test-Path $shaFile)) {
    throw "未找到对应的校验和文件: $shaFile"
}
$expectedSha = (Get-Content -Path $shaFile -Raw).Trim().Split()[0]

# 2. 在系统临时目录生成 GUID 独占沙箱目录
$scratchDir = Join-Path $env:TEMP ("g2v-release-gate-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $scratchDir -Force | Out-Null

try {
    # 3. 校验包结构与基础启动冒烟
    uv run --frozen --extra desktop python scripts/verify_windows_portable.py $archivePath
    if ($LASTEXITCODE -ne 0) {
        throw "verify_windows_portable 校验未通过 (退出码: $LASTEXITCODE)"
    }

    uv run --frozen --extra desktop python scripts/smoke_windows_portable.py $archivePath
    if ($LASTEXITCODE -ne 0) {
        throw "smoke_windows_portable 冒烟未通过 (退出码: $LASTEXITCODE)"
    }

    # 4. 执行端到端业务验收（使用 --no-dev 保证纯净运行）
    uv run --frozen --extra desktop --no-dev python scripts/smoke_windows_turn_reply.py `
        --archive $archivePath `
        --expected-sha $expectedSha `
        --scratch $scratchDir
    if ($LASTEXITCODE -ne 0) {
        throw "smoke_windows_turn_reply 业务验收未通过 (退出码: $LASTEXITCODE)"
    }

    Write-Host "便携包发布验收全部通过。"
}
finally {
    # 失败时请保留沙箱内的 result.json 与合成服务日志以供定位
    # 切勿删除真实用户 data 目录重试，严禁向外上传解压安装树、私有数据库或密钥
    Write-Host "验收临时目录: $scratchDir"
}
```

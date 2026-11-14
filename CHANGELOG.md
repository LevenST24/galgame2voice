# Changelog

All notable changes to galgame2voice are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]






### Fixed

### Removed
- 修复首次打开或刷新页面时，对话模型还在检查阶段标题画面就先显示「开始聊天」，未配置模型时点击会跳过首次使用的三步清单、进入无法发消息的对话的问题；现在检查结束前先显示「正在检查设置…」，得出结果后再对应显示三步清单或开始按钮。
- 修复导入的聊天备份中若含有 system 角色记录，后续继续对话时会被当作系统级指令原样发给大模型、可能改写角色设定的问题；现在只有用户与角色的对话会进入上下文。
- 修复某一轮对话没有生成语音（如关闭语音或台词只有动作描写）时，设置中心「运行状态」的阶段耗时里「首音就绪」误显示为整轮耗时的问题；现在没有生成语音的轮次显示为「--」。
- 修复旧 Telegram 连通性测试在途时重新打开设置或发起新测试后，旧响应结束时错误提前解锁新测试按钮并弹出旧提示的问题，以及测试进行中编辑输入框会导致测试按钮永久锁死的问题；现在 Telegram 测试拥有独立的代数守卫与 AbortController，草稿变更或重新初始化立即作废旧测试且不泄露提示或干扰新状态，编辑输入框会即时重置忙碌态以允许再次重试。
- 修复含有较长括号动作描写的短台词或未指定切分方式时，语音合成误按长句切分且无法复用相同台词的语音缓存的问题；现在按去除动作描写后的实际台词长度统一决定切分方式并复用语音缓存。
- 修复对话模型未按逐句格式回复时，该条回复在对话记录（LOG）中无法重新生成语音、且导出聊天备份再导入后后续对话会丢失这轮回复内容的问题；现在这类整段回复可正常重新生成语音，备份导入后对话内容完整保留。
- 修复自定义运行数据目录并在设置中选择语音引擎文件夹后，重新启动程序仍按安装目录里的旧位置拉起或查找语音引擎的问题；现在启动时会优先使用运行数据目录中保存的引擎文件夹。
- 修复刚切换音色还在后台预热时删除该音色可能被重新激活或未提示正在加载、删除音色后历史会话仍残留旧音色绑定，以及保存角色人设失败时角色包文件已被提前改写的问题；现在预热加载中会拒绝删除并提示稍后再试，删除音色会同步清空会话绑定，保存人设失败时角色包与当前设定保持原样。
- 修复自定义运行数据目录时，启动自动校准语音精度或退出程序时仍向程序目录读写精度缓存、进程记录及回退读取同名参考音频的问题；现在启动校准、参考音频查找与退出清理均只使用所配置的运行数据目录。
- 修复在设置中心测试模型服务连通性时，更换接口地址后仍保留已保存的掩码密钥、或填写未开启访问许可的本机与局域网地址时，未能给出明确中文原因（甚至误报“配置不完整或数值超出范围”）的问题；现在会准确提示具体原因并给出重填完整密钥或开启本机/局域网访问许可的操作建议。
- 修复自定义运行数据目录（通过 `GALGAME2VOICE_PROJECT_ROOT` 指定）时，在设置中心切换为远端语音引擎或点击「重启 SoVITS 引擎」仍向程序安装目录读写引擎缓存、日志和进程记录，导致自定义目录残留旧记录且引擎启动失败时读取不到对应日志的问题；现在本地引擎启停与日志诊断统一使用所配置的运行数据目录。
- 移除网页界面与 Telegram 均未使用的好感度、长期记忆和性能指标独立接口；对话中的长期记忆提取与好感度演进，以及设置中心的语音缓存统计与清理功能保持不变。
- 修复尚未配置对话模型时，标题画面「第一次使用」三步清单在语音引擎已就绪并打勾的情况下仍显示「暂时只用文字」按钮的问题；现在语音引擎就绪时不再显示该按钮，未就绪时仍照常显示。
- 修复在「设置中心 → 对话模型」中模型服务尚未填写密钥时，状态显示为「还不能用」易被误认为不支持该服务商、且下方提示没有申请入口的问题；现在状态直接标明「未填密钥」，并附上对应厂商官网的 API 密钥申请链接。
- 修复在「角色与会话」的「兑换码」输入框粘贴兑换码后按回车无反应、从聊天软件复制时夹带换行或不可见字符会校验失败，以及兑换码有误时直接显示底层技术报错的问题；现在支持按回车直接查询（输入法选字时不误触），自动忽略夹杂的空白与不可见字符，并在兑换码不完整或有误时给出清晰的重贴提示。
- 修复使用默认 DeepSeek 配置或已保存旧版 DeepSeek 模型（`deepseek-chat` / `deepseek-reasoner`）发起对话时因官方停用旧模型名报错，以及将「思考强度」设为「关闭」后仍会开启思考的问题；现在预设更新为 `deepseek-flash` 与 `deepseek-v4-pro`、默认关闭思考，并在启动时将旧模型配置自动迁移为现行模型。
- 修复「设置中心 → 角色与会话」中「角色音色」下拉框在较窄窗口下被同行的五个按钮挤压截断、首项使用技术术语「跟随后端当前音色」，以及未选中具体音色时「删除」按钮仍可点击的问题；现在下拉框独占一行、首项改为「不指定（沿用语音服务当前的声音）」，且仅在选中可删除音色时才可点击「删除」。
## [3.2.0] - 2026-11-14
### Added
- 「角色与会话」新增「导入角色包」，支持选择角色包文件夹或 `.zip` 压缩包、或直接拖入设置窗口自动校验、放入 `characters/` 并选中该角色。
- 「角色与会话」新增「兑换码」入口，粘贴 `G2V1.` 开头的兑换码即可查询、下载并自动导入和选中角色包，本机已有同名角色确认覆盖时无需重复下载。
- 设置中心「运行状态」新增展示最近 5 轮对话的端到端阶段耗时（首字、首句切出、首音就绪、首音播放），未生成语音的轮次在语音阶段显示为「--」。
### Changed
- 设置中心「备份与更新」检查更新时不再在应用内拉取并覆盖本地文件，改为提示 GitHub Releases 是否有新的正式版本并提供下载页链接。
- 从首页或角色入口新建对话时，默认语速优先采用该角色包配置的默认语速，无角色包时保持 `1.0`。
- 设置中心改为根据服务端下发的模型预设决定密钥是否必填、默认模型、推荐列表及默认思考强度。
- 新操作提示出现时会替换画面上已停留的旧提示，同一次操作中紧接着触发的多条提示仍一并保留。
- 自动扫描模型与参考音频时不再重复遍历数据目录与音频目录，音频文件较多时扫描更快。
- 统一界面用词与标点（「会话」「运行状态」「返回首页」等），并将音色管理、角色扫描与检查更新失败提示统一改为说明原因、影响与下一步操作的中文指引。
### Removed
- 移除 Telegram 机器人功能与设置中心里的「Telegram」页面，原有的 Telegram 历史对话与旧版聊天备份仍可在网页端正常查看、导出和导入。
- 移除设置中的「回复格式」选项与旧版残留无效的「日文原文显示」选项，新对话统一按逐句台词与演出生成，旧会话配置、历史整段消息回看及语音重生成不受影响。
- 移除旧的同步聊天接口（`POST /api/chat`）、独立语音合成接口（`POST /api/voice/synthesize`）及应用内覆盖更新接口（`POST /api/system/update/apply`），对话统一使用 `POST /api/chat/stream`，历史语音失效后可到 LOG 重新生成。
- 移除已无调用方的早期接口（`GET/POST /ai/chat`、`POST /api/chat/japanese`、`GET /status`、`GET /api/voice/fs-browse`、`GET /api/voice/presets`、`GET /api/providers/presets`、`GET /api/providers/{id}/models` 及独立的好感度、长期记忆与性能指标接口），服务存活检查请使用 `GET /api/health`，预设列表随 `GET /api/providers` 返回。
[Unreleased]: https://github.com/LevenST24/galgame2voice/compare/v3.2.0...HEAD
[3.2.0]: https://github.com/LevenST24/galgame2voice/releases/tag/v3.2.0
### Fixed
- 修复 1366×768 等桌面窗口打开标题画面时欢迎词被截断、看不见发送方式与设置中心说明的问题。
- 修复首次使用时语音未就绪、未填密钥、引擎目录少进一层、角色压缩包不合规或端口被占等场景下提示指引与画面按钮不一致的问题。
- 修复尚未导入角色包且未配置音色时，生成语音误引导去检查语音引擎的问题，现会提示前往「角色与会话」导入角色包或创建自定义音色。
- 修复尚未导入角色包且会话未绑定音色时，在「角色与会话」中无法直接保存温度、最大 Token 等常规会话参数的问题。
- 修复全新安装首次启动时默认生效的模型服务商与全局设置记录不一致的问题，现统一默认为 DeepSeek 且不影响已有配置。
- 修复启动时覆盖用户已保存模型名称、内置预设缺少 Moonshot、重置 Gemini/Claude 默认地址缺少路径后缀，以及模型名称留空时误按 OpenAI 默认模型请求的问题。
- 修复选用 Anthropic Claude 时未填密钥仍反复联网直至超时，以及发起对话同时发送温度与采样参数导致 400 报错的问题。
- 修复接入推理模型（如 DeepSeek-R1、QwQ 及使用 `<thinking>` 标签的模型）时思考内容泄露到字幕与语音朗读或误报空回复的问题，并在思考占满生成长度时明确提示长度耗尽。
- 修复模型回复触发内容安全策略、返回空内容、中途中断或超出最大生成长度截断时提示笼统的问题，现会说明具体原因并提示已收台词保留在 LOG 中。
- 修复对话请求遇到余额不足（402）、上下文或参数错误（400）、反向代理 502/524 或网络中途断开时误报密钥失效、泄露网页源码或误打开模型设置的问题。
- 修复设置中心「测试连通性」在更换接口地址仍留着掩码密钥、未开启本机/局域网访问许可、所填模型名不在远端列表或报错嵌有状态码数字时误判就绪或归因错误的问题。
- 修复在全局设置中仅切换模型服务商下拉项后直接点底栏「保存设置」时，新服务商未被保存与启用却提示成功的问题。
- 修复在「设置中心 → 语音服务」修改本机引擎目录后直接点「保存全局配置」时，引擎目录未保存却提示已保存的问题。
- 修复更改「计算方式」自动保存后仍被当作未保存修改，导致重开设置显示旧值并在保存全局配置时把旧值写回的问题。
- 修复非「语音服务」页面底栏仍显示「恢复默认」按钮，点击后会在后续保存时意外重置计算方式与音频缓存保留时间的问题。
- 修复检查出新版本后重新打开设置中心时状态被刷回「已是最新」，以及「检查更新」请求失败时无错误提示并残留旧横幅的问题。
- 修复使用 `--cpu`、`--fp32`、`--fp16` 或 `--precision` 启动服务后运行状态误提示待重启、网页重启丢失命令行模式、`--cpu` 日志误显示为 `FP32`，以及网页修改计算方式后跳回旧模式的问题。
- 修复默认「自动」计算方式下保存全局设置或点击「重启 SoVITS 引擎」会丢失启动时的显卡精度校准结果、退回静音 FP16 的问题。
- 修复仅配置自定义音色时启动精度校准找不到参考音频，以及静音显卡切回 FP32 重启后未等新引擎就绪就提前探测导致误报静音且不记住校准结果的问题。
- 修复无独立显卡的设备在「运行状态」误显示「切为 GPU(FP16)」快捷按钮的问题，现纯 CPU 模式下自动隐藏该按钮。
- 修复在系统繁忙或命令响应较慢的 Windows 设备上，查看运行状态或切换音色时显卡检测超时后一直卡住不返回的问题。
- 修复首次语音状态确认完成前、引擎启动校准加载期间、停止旧进程后端口未释放或重启报错时，首页与运行状态卡片误闪「未连接」「在线」或误报「加载完成前退出」的问题。
- 修复将受管语音引擎地址配置为 `http://localhost:9880` 等本机回环别名时，运行状态无法识别受管引擎且网页端无法正常重启的问题。
- 修复自动识别并保存同级放置的 GPT-SoVITS 引擎后，将整合包整体移动或复制到新位置时引擎连接失效的问题。
- 修复通过 `GALGAME2VOICE_PROJECT_ROOT` 自定义运行数据目录时，引擎文件夹记录、启动精度校准、参考音频回退及进程与日志管理仍读写安装目录的问题。
- 修复程序所在路径包含 `#` 或 `%` 时启动读不到已保存的语音服务地址且 `galgame2voice doctor` 误报数据库损坏，以及保存语音服务地址后 `doctor` 仍检查默认地址的问题。
- 修复系统环境变量存在其他 FFmpeg 时便携版启动自检报错或误用外部音频组件，以及便携包解压后缺少空 `backgrounds/` 目录的问题。
- 修复新建自定义音色时参考音频列表被语音缓存占满、含 `cache` 的用户文件夹被误忽略、`.pth` 模型放在 `data/` 等目录找不到，以及引擎位于盘符根目录或上级嵌套目录时被拒绝的问题。
- 修复角色包解析相对路径情感音频时反复扫描上级目录与各盘根目录拖慢启动，以及损坏角色包文件夹名超过 100 字符时扫描报 500 崩溃的问题。
- 修复角色包配置了自定义默认情绪时未优先选用该情绪参考音频，以及开启「自动调节语速与语气」时语音仍被下方固定滑块锁定的问题。
- 修复含较长括号动作描写的短台词误按长句切分且无法复用语音缓存，以及纯动作与标点台词触发流式语音合成报错的问题。
- 修复开启隐私模式（`GALGAME2VOICE_PRIVACY_MODE=true`）后对话没有声音的问题，现将语音写入临时文件供本次播放且不落盘缓存。
- 修复 GPT-SoVITS 引擎连接后无响应时，切换音色设置参考音频等请求一直等待并占住合成锁阻塞后续语音请求的问题。
- 修复对话合成自动加载会话音色后在设置中再次选择同一音色仍重复加载，以及切换音色失败后误标「当前加载」、无法删除该音色或删除后不恢复原当前音色显示的问题。
- 修复音色后台预热期间未拦截删除操作、删除音色后全局设置与历史会话同步仍残留旧绑定，以及音色列表加载完成前点保存会清空会话角色绑定的问题。
- 修复保存角色人设时遇到写库失败或请求中断却已提前改写角色包文件的问题，现会自动恢复原人设并在回滚失败时如实提示。
- 修复磁盘空间已满或数据目录只读时对话、语音合成、单句重生成、人设保存及音色管理误报密钥或引擎异常的问题，现统一明确提示存储原因并在单句重生成失败时保留原音频与清理残留文件。
- 修复角色包立绘文件名或服装文件夹名包含 `%20`、`%2B` 等百分号编码字符时立绘加载报 403 无法显示的问题。
- 修复早期未分句历史回复、模型未按逐句格式回复的整段消息及仅合成部分语音的台词无法在 LOG 中重新生成语音，或整段回复导出再导入后丢失内容的问题。
- 修复两个标签页先后重新生成同一句台词语音时后提交页直接报 409 冲突且无法同步已生成语音的问题。
- 修复在 LOG 中等待单句语音重新生成期间，点击其他条目的「语音」或「播放录音」会静默取消本次生成的问题。
- 修复欢迎语等不配音台词在 LOG 中仍显示不可用的语音按钮，以及已播过的重生成语音在后续操作后重复出现「点语音播放」提示的问题。
- 修复在 LOG 播放历史台词或刚重生成的语音期间列表刷新导致「语音」按钮丢失播放高亮，以及录音缓存缺失时「播放录音」按钮一直停留在播放态的问题。
- 修复发送提问后在角色开始回复前主动取消或刷新重开网页（含超过 100 条消息的长会话）时提问无法在 LOG 重新发送，以及后端已保存提问但中途断连重开后同一提问重复显示两条的问题。
- 修复浏览大量历史会话导致浏览器本地存储写满后无法保存新消息或清空内存历史记录的问题，现会优先释放较早未打开会话的本地缓存并在存储仍满时保留内存记录供查看与导出。
- 修复长时间不刷新网页时浏览器语音缓存只在首次打开时清理一次的问题，现每次缓存新语音后均只保留最近 100 条且不删除用户录音。
- 修复尚未打开过的同步历史会话或最新回复仅有日文台词时，标题页摘录与「会话」抽屉列表误跳过该轮或显示「暂无消息」的问题。
- 修复首页「全部会话」列表定时刷新时整页重建导致键盘选中项丢失焦点、缩略图反复重新加载的问题。
- 修复会话处于「跟随后端当前音色」时后端当前音色变更后舞台未同步更新角色，以及对话中同一条无效角色包目录告警每次发消息都重复弹出的问题。
- 修复导入的聊天备份中含有 `system` 角色记录时后续对话将其原样作为系统指令发给模型的问题，现仅将用户与角色的对话送入上下文。
- 修复服务端清空会话历史后网页刷新仍显示旧消息并在导出备份时产生多余「浏览器副本」，以及全局设置与备份混入会话级 `advSubtitleLanguage` 字段的问题。
- 修复早期创建或导出的会话仍显示旧版默认欢迎语引导不存在入口的问题，现加载或导入时会自动刷新完全匹配的默认欢迎语且不影响真实对话记录。
- 修复聊天输入框超过 4000 字发送时清空草稿且报错失真，以及打开设置中心时未立即停止首页正在播放的试听语音的问题。
- 修复在输入框使用中文/日文输入法组字时按 Esc 取消候选词会直接关闭弹窗，以及在背景选择对话框按 Tab 或 Shift+Tab 时焦点跳出对话框的问题。

## [3.1.2] - 2026-11-04

### Fixed

- 修复 Windows 便携版浏览器验收在初始页面尚未完成导航时执行脚本、因 CDP 上下文被销毁而失败的问题；先有界确认目标页面就绪，再执行一次完整业务验收，就绪阶段的重试不会重放业务操作。

## [3.1.1] - 2026-11-04

### Added

- ADV 人物可随台词与情绪改变站位和远近，支持减少动态效果。

### Changed

- 依赖审计发现问题时会阻断 CI 与合并，不再把整个审计作业设置为允许失败；保持原有审计命令与阈值。
- LOG 浏览器回归改为有界等待界面可达状态，覆盖普通动画、减少动态效果与延迟初始化，并完整显示失败原因。
- 统一聊天舞台、记录与设置等界面的夜色视觉风格，提升浅色背景下的台词可读性。
- 将玩家默认名改为“我”，并兼容旧会话中的默认名“Player”。
- 重新整理对话记录界面，限制阅读宽度、改善中日文及语音操作的层级，并提高运行状态卡片的可读性。
- 重排聊天舞台：沿用首页的衬线台词、强调线和轻量浮层，扩大场景与角色的视觉空间。

### Removed

- 移除手机竖屏专用布局，竖向窗口显示等比缩小的横屏舞台。

### Fixed

- 修复自适应音色在按情绪匹配参考音频时，未按规范情绪名与同义词映射导致无法匹配对应音频并回退到默认参考，以及角色包清单自定义情绪可能被覆盖的问题；现在支持通过情绪规范名、同义词及角色包清单自定义键值精确解析情绪参考音频。

- 修复 Telegram 机器人在语音转录、大模型生成或排队期间收到 /reset 指令或控制台清空按钮时，重置响应后旧回复仍继续发送、孤儿入库、误增好感度并触发后台语音合成，且旧转录可能阻塞新轮次或穿透报错的问题；现在会话重置会立即作废并取消当前成员正在转录、生成与排队中的旧轮次，旧回复、音频与记录不再回流。
- 修复流式回复被中断或提前取消时，已完整合成但尚未发送 SSE 事件的语音行在保存快照时被误标为 failed 的问题：已就绪行正确保存为 ready，未合成完成或出错的语音行仍保留为 failed。
- 修复全局设置中测试模型连通性时缺乏请求归属校验，导致切换模型、修改配置草稿或关闭设置后晚到的结果覆盖当前界面、且旧请求结束时可能提前解除新测试繁忙状态的问题：为连通性测试建立草稿快照、取消控制与代数守卫，切换提供商、修改参数草稿或关闭设置时作废旧测试结果并立即恢复测试按钮，保持新测试的繁忙状态不被旧请求打断。
- 修复读取指向模型权重的指针文件时不支持 UTF-8 BOM 的问题；现在解析 `.ckpt` 与 `.pth` 指针时兼容 UTF-8 BOM，并保持严格的路径遍历防范。
- 修复新建音色档案时名称或权重路径仅填写空白字符仍允许创建、导致数据库残留无效记录或切换异常的问题；现在创建音色时对空白名称与权重路径进行校验并拒绝保存。
- 修复音色切换与回退时参考音频、参考语言与提示文本归属脱节的问题；统一区分角色包自带参考、跨包冲突与自定义外部参考，回退时联动更新参考音频、语言与文本，并防止音色缓存混用错误权重。
- 修复参考音频时长预检对相对路径和绝对路径处理不一致、且可能误借用其他角色包同名短音频的问题；现在仅从明确合规位置解析参考文件，确保自定义音频时长判定准确。
- 修复源码版更新时若备份失败仍继续执行清理导致文件丢失，以及更新过程中若前端构建未完成或服务重启后更新状态丢失的问题；更新前备份失败立即中止并保留未知静态文件，同时持久化构建待办状态，源码版可在界面中直接重试构建与更新（便携版不适用在线更新构建，仍通过新版发布包整体替换）。
- 修复流式回复被中断后在 LOG 重新生成语音时因缺少精确服务端助手消息关联而无法匹配或刷新后丢失的问题；数据库新增 `reply_to_message_id` 关联与索引，提供轮次回复定位接口，确保中断回复的单句语音在重新加载历史后仍可准确重试与持久化。
- 修复 Windows 便携版打包脚本处理带有反斜杠或混合路径分隔符的第三方库 LICENSE 元数据时出现复制错误的问题。
- 修复在设置中清空 Telegram 允许用户 ID 时，若未禁用 Telegram 则可能静默失效或无法彻底清空的问题；现在启用状态下清空会明确提示拒绝，禁用状态下可正确保存清空后的白名单。
- 修复 OpenAI 与 Anthropic 流式对话在遇到连接意外断开或达到最大生成长度（max_tokens / length）时被误判为正常结束并结算好感度的问题；现在准确识别未完成与截断流，保留已生成的文本内容并提示重试建议，且不结算该轮好感度。
- 修复导入聊天备份与并发删除同一对话时，因迟到的删除响应导致刚导入的同 ID 对话被意外删除的问题；建立导入与删除的操作互斥与版本校验，保证恢复数据不被迟到删除覆盖。
- 修复启动器在自动切换端口或通过环境变量启动时，地址与端口别名未完全同步导致配置缓存与生命周期安全守卫读取到陈旧绑定地址的问题；启动时统一同步全部主机与端口别名并刷新设置缓存。

- 修复流式回复已经收到部分台词后等待超时或连接中断时，舞台停在错误框、文案却承诺重新发送的问题；现在说明台词留在 LOG 里，并可点「继续」回到输入。
- 修复保存角色包人设提示词时，在主事件循环中同步执行全量角色包扫描导致扫描完成前其他请求被卡住的问题；现在扫描改为在后台线程中执行，保存期间其他请求可正常响应，并保持多次保存的串行顺序与失败回滚不变。
- 修复用 Windows 记事本等工具保存带 UTF-8 BOM 的角色包 `manifest.json` 或 `system_prompt.txt` 时，角色包被当成损坏拒绝、备用人设开头带入不可见 BOM 字符且保存人设接口返回 500 的问题；现在读取角色包清单与人设文本均兼容 UTF-8 BOM，保存人设仍以无 BOM 的 UTF-8 写回。
- 修复回退分句模式下回复出现超过 10000 字的超长句或合并尾句时，整条消息的分句记录在保存时被清空、导致该条回复中任何一句在 LOG 重新生成语音都返回 404 的问题；现在超长句会按句末标点、逗号或长度上限切分为多句保存，不丢字且不改变正常长度句子的切分结果。
- 修复库里存在中文内容为空的助手消息时，查询任意一句中文的日文对照都会被这条空消息劫持、返回无关日文的问题；现在按中文查日文时会跳过中文内容为空的消息，找不到匹配时仍交给模型翻译。
- 修复导入的聊天备份中单句台词的演出属性（act）含有空值、空字符串、错误类型或未知枚举值时，整条回复的分句在重新加载历史记录或导出完整备份时全部丢失的问题；现在导入时会自动剔除不合规的演出键值，保留合法演出与完整分句。
- 修复流式语音合成开头存在超过约 0.51 秒自然停顿或静音时，预缓冲过早将整段音频误判为全静音并抛错截断的问题；现在将观测门槛放宽至约 2 秒，自然停顿后接正常语音的流式音频可完整输出。
- 修复推理模型长时间只输出思考内容时，回复约两分钟后被当成网络故障中止的问题；思考期间会发送不含思考正文的进度，台词、语音和记录仍只保留正文。
- 修复本机引擎加载较慢导致端口超过 120 秒才打开时，监视线程提前退出并永久跳过静音校准、导致引擎可能持续停留在静音 FP16 模式的问题；现在监视线程会在子进程存活期间持续等待端口就绪，并在端口就绪后照常触发精度校准。
- 修复 FP16 静音自动校准换进程时，未作废音色缓存导致新进程漏发模型权重、空窗期状态误报为启动失败、以及端口未释放时仍强行拉起新进程的问题；现在换进程前清除音色缓存、空窗期复用重启锁保持启动中状态，且端口未释放时中止拉起并记录警告。
- 修复修改 GPT-SoVITS 引擎地址后，旧 HTTP 连接池排空任务因与新地址共享在途请求计数而在新地址持续有流量时被迫等待满 330 秒硬超时、并误报强制关闭警告的问题；现在在途请求按连接池实例独立计数，旧连接池在自身的请求排空后即可立即安全关闭。
- 修复自定义音色名称带括号后缀（如「安娜 (V2)」）时被当成原角色的问题；现在按这个名字选人、保存人设和配音都使用该音色自己的档案与参考音频，不再改写原角色包的人设。
- 修复保存人设时角色包和数据库不一致的问题：角色包 ID 重复等扫描失败会还原 manifest.json、数据库保持原人设，并直接说明冲突原因；两个标签页先后保存结束后，磁盘和数据库始终是同一次保存的文本。
- 修复更新音色时名称或权重路径只填写空白仍会保存、把原音色名称或模型路径写成空白的问题；现在会拒绝这次更新并保留原记录。
- 修复自定义音色的参考音频文件缺失、且路径里有名为 default 的目录时，会悄悄换用默认角色包里同名音频的问题；现在会正常提示缺少参考音频。
- 修复语音请求排队期间模型或参考文件变化后，可能返回旧缓存音频的问题；现在会提示重新选择音色。
- 修复已启用音色只改了参考语言或合成语言后，再次切换仍被当成已经加载、引擎继续用旧语言的问题；现在语言变化会重新下发给语音引擎。
- 修复会话绑定的音色已删除时，单句语音合成误报读取故障并提示检查引擎的问题；现在引导重新选择音色。
- 修复其他客户端删除对话时，排队发送的消息可能重新创建已删除对话的问题。
- 修复项目目录迁移或便携备份恢复后，数据库里的语音缓存仍指向旧绝对路径时，超过 7 天的缓存无法删除并反复出现警告的问题；现在会删除当前缓存目录中的对应文件并清掉过期记录，缓存目录之外的文件不会被删除。
- 修复 LOG 单句语音重新生成耗时超过一分钟时过早中止的问题，延长等待时间以容纳模型准备和推理。
- 修复冷启动时角色包 ID 重复导致已有角色列表无法读取的问题；显示冲突提示，修正后可重新扫描。
- 修复设置了 `GALGAME2VOICE_MIN_FREE_VRAM_GB` 后，切换音色的常规显存预检仍按 0.45 GB 放行的问题；现在未显式指定阈值时按该环境变量拒绝显存不足的加载，未设置时仍使用 0.45 GB。
- 修复用 aliases 给角色包改名后重新扫描，数据库里的音色仍是旧名、删掉旧别名再扫描会新建重复档案并与原会话和好感度脱钩的问题；现在重扫会把原档案改成新名字，档案 ID 不变。
- 修复会话绑定的音色已删除时，保存其他参数可能静默清除原绑定的问题；保留绑定并提示重新选择。
- 修复参考音频放在项目目录外的 GPT-SoVITS 目录时，创建音色可以成功，单独合成却因路径越界返回 400 的问题；现在合成校验使用与创建音色相同的允许目录。
- 修复浏览器暂未允许播放语音时，LOG 中的语音按钮被误禁用、无法再次点击的问题。
- 修复流式回复期间对话被其他客户端清空或删除时误报网络故障的问题，现会提示本次回复未保存。
- 修复在设置中将 GPT-SoVITS 服务地址改为远端后，本程序拉起的本机引擎进程继续在后台运行并占用显存的问题；现在生效端点变更为非本机时会主动停止本程序拉起的本机引擎并清空受管记录以释放显存，保存本机地址或用户外部启动的引擎不受影响。

- 导入包含逐句台词的聊天备份时，逐句数据以前没有存入数据库，换浏览器或重新加载历史后会丢失；现在会一同保存。

- 修复打开玩家输入框时，上一句的点击等待图标仍显示的问题。
- 改善对话记录中单句语音生成状态和错误提示的阅读空间。
- 修复语音开始前关闭页面或传输时断开连接后，语音任务可能继续占用引擎资源的问题。
- 修复引擎重启或离线切换后，Telegram 控制台再点当前音色会提示「已经是当前音色」且不加载权重的问题；现在会确认引擎确实加载了该音色，没有加载就照常切换。
- 修复音色缺少模型路径时可能使用上一个角色模型的问题；现在会提示补全 GPT 和 SoVITS 路径。
- 修复选中音色缺少参考音频时可能借用上一角色声音的问题；语音流在开始返回音频前会给出配置提示。
- 统一音色管理器与合成服务的选音逻辑，避免旧音色信息覆盖明确指定的角色或旧版音色 ID。
- 修复自动清理音频缓存时可能误删用户录音的问题；只按数量清理可识别的生成语音副本。
- 修复流式聊天中途修改音色后可能混用旧参考的问题；音色参考文本为空时不再借用引擎中上一角色的文本。
- 修复回复提交成功时请求恰好取消，可能重复保存回复或删除对应提问的问题；删除、清空或重建会话后，旧请求不再写回该会话。
- 修复音色配置在读取期间发生更新或删除时，当前语音请求仍可能使用旧配置的问题；连续更新时提示稍后重试，避免误用其他音色。
- 修复音色还在加载时被删除，引擎留在已删除音色、当前启用音色回落到默认角色的问题；正在加载的音色会拒绝删除，若加载结束前音色已不在，则恢复原来的启用音色并提示重新选择。
- 修复单个对话界面事件处理出错时可能阻断其他监听器、发送或回复保存的问题；错误仍会记录到浏览器控制台。
- 修复首页状态读取较慢时，关闭后重新打开首页会被旧请求阻塞的问题；迟到结果不会覆盖新状态。
- 模型请求因临时故障自动重试时重新检查服务地址；若域名已解析到禁止的私网地址，停止重试并提示检查配置。
- 更正模型服务地址安全说明：记录请求前重新检查及 DNS 重绑定、系统代理解析的限制。
- 修复模型配置读取失败被误报为“未配置”的问题；首页保留现有对话并提示状态未确认，读取恢复后重新显示真实状态。
- 修复已有聊天记录时，模型未配置或配置读取失败会隐藏进入当前对话入口的问题；仍可从首页查看历史。
- 修复设置为等待语音播完再翻页时，点击下一句后关闭语音会丢失翻页操作的问题。
- 修复等待语音时打开设置并关闭语音，返回对话后仍会等待合成、无法立即看到文字的问题。
- 启动器从选定端口到服务器接管期间持续持有监听套接字，避免端口在探测后被其他程序抢占。
- 修复 Windows 已有服务启用端口复用时，启动器可能把占用端口误判为空闲的问题。
- 自动打开网页时等待较慢的服务启动，核对健康响应是否属于本程序；等待超时或浏览器无法自动启动时显示可手动访问的地址。
- 修复绑定 IPv6 地址时的端口探测和浏览器网址，以及选择自动端口时错误显示端口 0 的问题；重复启动会使用对应的本机 IPv6 地址。
- 更正便携版自检说明，使用可在 PowerShell 和命令提示符中运行的当前目录路径。
- Windows 依赖恢复提示分别提供命令提示符和 PowerShell 可执行的命令，并正确处理路径中的特殊字符。
- 依赖安装命令结束后重新确认当前 Python 能加载缺失的库；安装到其他位置时明确提示，不再误报成功。
- 修复依赖安装失败后的命令提示：带空格的路径会正确加引号，手动安装始终使用本次启动的 Python 环境。
- 修复源码启动时缺少 Pydantic 或版本不兼容导致依赖检查前直接报错的问题；现在会给出项目虚拟环境和 pip 的安装指引。
- 修复源码版 Windows 启动脚本忽略项目虚拟环境的问题；存在 `.venv` 时优先使用其中的 Python，避免误用另一套系统依赖。
- 修复语音连接已建立后在合成中途断开，后续重试仍信任旧模型记录的问题。读写连接错误、服务端提前关闭响应与连接失败共用失效和恢复边界；已播出部分音频的流不自动重播，下一次请求重新准备目标音色。
- 优化长上下文的 token 估算：按块选择字符计数方式，限制临时内存，并保留原有中日文字范围、Unicode 空白处理及取整规则，避免交替文本出现性能退化。

- 修复语音引擎在同一地址断连重启后仍沿用旧模型记录的问题。连接错误、连接超时、重启或退出会使加载记录失效，下一次语音按目标音色重新准备；读取和连接池超时不触发重复加载，迟到探测不覆盖新的远端模型确认，本地跳过加载不算远端确认，探测结果保持原地址归属。

- 修复取消语音任务后复用任务标识时，旧任务收尾移除新任务登记、取消旧轮次误伤新轮次的问题。预热及同句合成使用独立完整任务标识，仍在使用的显式标识不再静默覆盖，避免取消和退出清理遗漏任务。

- 将便携构建链中锁定的 setuptools 从 84.0.0 更新至 84.1.0，修复 [GHSA-xmj3-9gf7-h52w](https://github.com/advisories/GHSA-xmj3-9gf7-h52w) 涉及的 Windows 旧式 wheel 安装路径越界问题。

- 启动时不再把数据库里的默认权重直接标成引擎已加载，离线、健康检查失败及首次加载失败都保留未确认状态。后台预热的模型加载和探测统一进入低优先级队列，执行时重新确认当前音色，避免迟到预热覆盖用户的新选择；预热探测可取消，并沿用模型加载所需的较长超时。

- 启用音色时确认数据库确实保存成功；只读、写入异常和配置记录缺失不再被误报为切换成功。保存失败保留已加载模型和会话绑定，显示中文说明及“重新选择音色”入口，用户可重试保存且无需重复加载模型；REST 与 Telegram 使用同一确认逻辑。
- 首次启用数据库默认音色时仍实际加载模型，不再把保存的默认 ID 填成引擎的已加载状态而跳过加载。保存失败后清除前端已确认启用状态，迟到列表不再恢复错误标记。
- 聊天体验保存会等待本机服务确认，失败时保留编辑内容并允许主动重试，保存期间重复点击不再重复写入。继续编辑时只确认已提交的快照；切页、返回首页或重新打开设置后，旧结果不会关闭或覆盖新窗口，离开后的保存失败仍有明确提示。字幕切换等快捷操作的保存失败也不再静默忽略。
- 单句语音开始通知同时核对音频上下文与播放时钟；浏览器暂停、中断或音频时钟尚未到点时，不再仅凭页面计时器提前出字。恢复播放后只通知一次，停止或换会话会清理等待任务。
- 手动切换音色的内存预检和切换前后清理移到工作线程，音频定期清理也不再同步阻塞异步请求；显存检查只使用原生命令或已经完成加载的框架。引擎确认切换后，取消请求会先完成清理和所要求的启用音色保存，再释放切换锁；切换前取消不会继续加载模型。
- 新会话欢迎语改为当前设置中心的分类与返回开始页路径，移除旧 CONFIG 底部入口指引；保留旧欢迎消息识别，聊天备份不会把它误判成新增对话。
- 运行状态页在原生显卡查询失败时不再冷导入 PyTorch，也不等待其他线程尚未完成的框架导入；计算下一次启动方式复用同一次显卡采样。无法验证硬件默认值时保留“待确认”，已保存的 CPU/FP16/FP32、环境和引擎配置仍按原优先级解析，实际引擎启动保留完整探测路径。
- 语音同步模式等本句声音真正开始才出字；通常模式在音频可播放时出字，切句时清除前句。AUTO 等待迟到语音及播放结束；语音失败、跳过、关闭语音或回复结束而没有音频时继续显示文字，保留手动先看文字与按阅读速度播放。设置中的聊天体验、角色与会话改为同一窗口内的分类页，统一关闭、键盘焦点与固定的返回开始页入口；切页保留角色草稿，关闭或换页还原未保存的体验预览，迟到的角色读取不会重新打开已退出的页面。
- 聊天遇到已失效的音色、角色或指定模型配置时明确提示，不再静默借用其他全局角色或默认模型；聊天与 TTS 共用唯一身份匹配，明确角色名优先于会话默认绑定。保留未指定时的默认选择与明确名字备用项；确认音色删除后也不自动清空绑定。失败配置不启动记忆提取，错误入口分别指向角色与会话或对话模型；重新选择后保留明确重试原提问的入口，新输入草稿仍优先。
- 流式回复按实际进展计算等待时限，保活心跳、空包和未完成 JSON 不再让无进展请求无限等待；超时保留已收到内容并取消读取，不自动重发。语音任务由后端公告并使用覆盖排队、双模型切换与推理的独立总预算，默认十分钟；正常慢语音不被模型的两分钟等待误杀，重复任务公告不能延长上限。旧后端保留有界兼容等待。
- 修复同一会话的后一条回复在排队等会话锁时只收到保活、约两分钟后被当成网络故障中止的问题；排队期间会发送不含台词的进度。
- 旧格式整段回复及仅在终稿提供的逐句台词现在会显示到聊天舞台；终稿补齐缺失的台词与音频结果，不重复播放已经收到的句子。只有日文的有效回复也会保存并显示；旧格式的临时展示台词不写成服务端行编号，历史单句修复继续使用真实行身份。
- 等待语音生成或解码时，缩放窗口与切换字幕语言只更新布局，不再提前显示整句台词；打开设置或下一轮思考期间也不再重新写回上一句。文字仍由播放开始、原有预览期限或手动推进显示，正常重排不触发语音重播。
- 兼容 Python 3.10 的请求取消与任务关闭；重复取消先完成本次任务和未发布音频的清理，再向调用者传递取消，避免状态读取或单句语音重建因版本差异失败。
- 设置页关闭、切到后台或被新一轮刷新替代时取消三路状态读取；旧缓存统计和运行时间不再覆盖新页面。引擎就绪状态独立显示，不等待目录或硬件统计；重启响应丢失后的未确认提示继续保留，避免在线状态被误当成重启参数已经生效。
- 系统诊断在并发读取时复用同目录扫描与静态显卡探测，缓存期限从完成时开始计算，目录结果最多保留 64 项。HTTP 断连回收请求自己的探测任务，共享测量由应用持有并在退出时关闭；数据库路径元数据检查移到工作线程，慢磁盘不再阻塞事件循环。
- 显卡状态优先复用已加载的 PyTorch，或使用 nvidia-smi、Windows WMI/CIM、Linux lspci 查询；原生查询已有结果时不再为状态页面冷导入整套 PyTorch。缺少原生信息时仍可回退，已加载框架的设备顺序继续保留。
- LOG 重新生成的单句语音按七天保留，不再被默认半小时的临时音频清理提前删除；取消、空音频及历史冲突会清理本次未采用的文件。音频文件改为独占创建，磁盘写入失败或工作线程在取消后完成时清理半文件；已提交或提交结果未确认的历史音频继续保留，注册的参考音频仍受保护。
- LOG 中无法播放或已过期的台词支持“用当前音色重新生成”，仅合成选中的日文句子，绕过坏缓存并生成独立音频；可取消、不自动播放、不重发整轮对话。服务端核对原台词与音频版本后只更新这一句，保留聊天时间和其他台词；未绑定音色时引导选择，指定音色失效时不再借用引擎当前角色。
- 浏览器缓存音频无法解码时，单句、流式及预先下载 Blob 的播放入口共用恢复流程：优先使用同页面较新的有效副本，否则最多补发一次绕过缓存的 GET；下载与解码沿用原总时限。坏副本按版本移除，迟到写入、读取及预取不再覆盖新副本，不清空麦克风录音；再次解码失败给出中文提示并保留文字，不自动重发语音合成 POST。
- 语音缓存读取中断或意外提前结束时明确终止流，保留原文件并暂时绕过出错版本；后续合成请求可重新生成，较新的有效文件不受旧读取错误影响。Windows 文件占用改为有界重试，失败后不再发布未写入的音频或元数据，文件合成可返回新的临时音频。
- 流式语音请求在调度队列中等待后重新检查同一音频缓存；前一个普通或文件合成已完成时直接分块复用，不再重复推理或重写缓存。关闭缓存和不同台词仍独立合成，中途取消可释放读取及调度任务。
- 同步更新语音、模型、聊天备份及首次欢迎消息中的设置入口名称，错误指引统一指向当前可见的“设置中心”和“角色与会话”。
- Windows 便携包的浏览器验证改为检查由 JavaScript 挂载的舞台和可见首页，不再凭旧版静态文案判定启动成功；浏览器使用独立的较短临时配置目录，保留失败诊断。
- 浏览器音频尚未唤醒时，停止、返回首页、切换会话或关闭播放器也能立即取消该句的准备任务；迟到的唤醒结果不再触发旧语音下载或覆盖新一句播放。每句只进行一次有时限的唤醒尝试，被浏览器拒绝时明确提示从 LOG 重播，文字仍可继续。
- 主界面的设置按钮与 F8 统一进入设置中心，明确区分聊天体验、角色会话和模型服务；移除角色面板里重复的显示设置。各设置面板与聊天界面均提供返回首页，保留聊天记录和输入草稿；修复首页遮挡聊天体验面板以及离开首页后快捷菜单仍不可点击的问题。
- 切换语音引擎地址或端口后，启动状态、等待时长和失败日志仅使用匹配该地址及当前进程的启动记录；状态探测期间发生重启时重新核对，避免把旧进程的响应误报为新引擎已就绪。状态卡同时显示本次探测的地址。
- 重启到新端口前先检查占用情况；新端口被其他程序使用时保留原本可用的受管引擎，不先停止旧引擎或改写计算方式。
- 引擎就绪轮询遇到短暂断连、限流或服务器错误时，在原有总时限内继续只读核对并显示“待确认”，不再直接误报启动失败。重启响应丢失后不重发重启请求，也不把当前在线误当成重启参数已生效；鉴权拒绝、明确的引擎失败仍立即提示。
- 修复健康检查遇到一次连接抖动就把已加载音色当成失效的问题。同一探测路径会先重试一次，连续两次仍失败才视为引擎重启并清掉已加载音色；网络偶发抖动不再让已加载的音色被当成失效。
- 设置页打开期间每五秒自动核对运行状态，关闭或隐藏页面后停止刷新；启动时看到的“正在加载”会随引擎就绪更新，不再一直停留在旧提示，关闭后迟到的响应也不会重建刷新任务。
- 同一页面的全局设置保存、计算方式自动保存与引擎重启按操作顺序发送，避免较早的保存覆盖新选择。保存结果无法确认时停止尚未发送的排队操作并保留输入；模型保存与全局保存部分成功时分别说明结果，不自动重发写请求。
- Windows 便携包只保留一份 `tools/ffmpeg.exe`，不再把源码环境的 imageio-ffmpeg 备用二进制重复冻结进内部目录；归档校验会拒绝重复 FFmpeg，音频转换与缺失工具诊断继续通过便携包验证。
- 保存计算方式只更新下次启动的设置，不再改写原引擎 YAML。运行状态按匹配的受管进程记录本次启动参数，明确标注待重启设置；外部引擎显示待确认，避免保存 CPU 后把仍在运行的 FP16 误报为 CPU。启动器同时按实际数据库路径读取设置，支持自定义数据目录。
- 源码发布 ZIP 改为按明确的程序目录和文件类型打包，排除角色、参考音频、引擎缓存、本机配置与工具私有目录；不跟随符号链接或 Windows 目录联接。压缩中断时保留上一份完整 ZIP 和校验文件。
- 将 GPT-SoVITS 的编译缓存、临时文件和启动配置副本放到 G2V 数据目录；兼容旧入口脚本硬编码 TEMP 的行为，不修改原引擎文件，避免只读引擎目录导致启动停滞或计算精度设置未生效。错误诊断只分析本次启动的日志，避免拿旧错误解释新故障。
- 语音引擎启动时同步更新状态标签与加载提示，避免同时显示“离线”和“正在加载”；迟到的状态刷新不再覆盖重启进度。超过三分钟仍未就绪时明确显示“加载较慢”，引擎日志改为即时输出，便于定位启动停滞。
- 首页长摘要保持原有字号，中文最多显示两行、日文最多一行，为按钮和最近会话留出空间；完整内容仍可在历史中查看。
- 模型连接诊断不再把所有连接失败误报为端口关闭；DNS 失败保留对应提示，设置页展示具体连接问题。
- 首页遇到一个还没有任何台词的会话（比如第一条消息回复到一半就被中断）时，不再显示空的引号「」，改为显示「从这一刻，开始你们的故事。」。
- 已绑定完整音色的首句合成会在同一推理锁内准备模型，再发送音频请求，避免切换尚未完成时直接报模型不一致。普通、文件和流式合成共用此流程；自动准备保留内存/显存检查、文件版本与引擎地址核验，不改写用户保存的全局音色，缓存命中也不触发模型加载。
- 快速切换会话或首页预览时，音色请求按顺序执行并跳过过时的排队选择，避免旧请求最后切回错误角色。进入聊天会核验实际语音模型状态；迟到的音色列表不再覆盖切换结果，列表暂时读取失败也不阻止按已绑定 ID 恢复音色。
- 模型保存已确认启用时不再重复发送启用请求，避免模型已经生效却提示失败。非密钥设置的保存响应丢失后只读核对一次；新 Key 无法从脱敏值确认时保留输入并说明结果未知，不自动重发保存。
- 模型设置保存期间继续输入的 Key、地址、模型名和思考强度会保留为未保存草稿；旧列表响应不再覆盖新状态，恢复默认地址也能随页脚保存。保存按钮共用忙碌状态，模型与全局配置部分成功时分别说明结果。
- 语音缓存身份加入数据库、引擎地址、完整参考音频路径及模型和参考文件版本；更换文件或数据库后不再复用旧音频。排队合成在获得引擎锁后核对音色，模型加载取消或回滚失败后保留未确认状态，重新选择音色可恢复；历史音频继续保留。
- 音色上下文按数据库和完整选择条件隔离，事务提交后刷新；并发读取共用加载，过期结果不会重新写入缓存，失败可重试。
- 明确指定的角色名与当前音色冲突时显示配置错误，不再借用当前角色声音；显式缓存数据库同时用于解析音色。
- 空角色扫描会刷新已移除角色包的情绪引用并保留用户音色；文件系统准备移出事件循环，未改变的角色包也会刷新清单变化。
- Windows 下生成的 JavaScript 和 CSS 使用 LF 换行，保持构建产物一致。
- 角色查找和音色同步只接受精确 ID、名称或无歧义别名，不再通过子串或通配符误匹配其他角色；冲突时保留原配置并提示，扫描失败不会部分覆盖目录。

- 语音预热使用有效文本「はい。」，避免引擎拒收单独的标点。
- 修复保存全局配置时响应中断或超时后，回读确认因提交了服务端已不再保存的记忆开关和昵称而必定失败、误报保存失败的问题；现在只提交当前配置项，回读一致即视为已保存。
- 修复本机语音引擎仍在加载或待核对时，首页显示红色「语音 未连接」的问题；现在显示黄色「语音 加载较慢」或「语音 待确认」，只有断开或失败才提示未连接。
- 修复在 LOG 里重新生成单句语音期间刷新对话记录后，「取消生成」变回重新生成按钮、完成或失败提示也看不到的问题；现在刷新后仍可取消，结果提示显示在这一句上。
- 修复 LOG 单句语音已写入服务端、但浏览器本地保存失败时，提示仍引导导出聊天备份的问题；该备份不含语音。现在说明语音已保存在服务端，本地缓存未更新。
- 修复音频缓存重拉后仍无法解码时，提示让用户再点 LOG 里已被禁用的「语音」按钮的问题；现在显示“这句语音无法解码，原有内容已保留”的专用说明。
- 修复在 LOG 里为当前这轮台词重新生成语音后，回到舞台重播当前句仍无声或仍播放旧音频的问题；现在会播放新生成的语音，舞台正在等待的后续句回到舞台后也会播放新语音。
- 修复在生成回复期间刷新页面后，服务端已入库的助手回复在前端不再出现的问题；启动同步时若服务端消息数多于本地，会从服务端加载最新历史补齐回复，同时保留本地消息与麦克风录音。
- 修复服务过载（503/529）、接口路径 404 或本机模型读响应超时时被误诊为模型名称不存在或要求检查密钥的问题；现在服务过载提示服务繁忙稍后重试且不要求修改配置，无模型字样的普通 404 提示检查 Base URL 接口路径，本机模型读超时提示本地服务响应较慢或可能仍在加载。
- 修复保存会话设置后服务尚未确认、按提示刷新页面时，服务端较旧的主角名、窗口不透明度和温度盖掉本机刚保存的设置的问题；现在启动同步会保留较新的本机设置，并在同步结束后补发到服务端。
- 修复导出完整聊天备份时，浏览器只保存了数据库消息的前半段，却用较旧的主角名、窗口不透明度和音色绑定换掉数据库里较新设置的问题；现在比较更新时间，保留较新的那份设置。
- 修复另一个网页改过聊天记录后，本页发第一条消息、保存显示设置、新建对话、绑定或解除音色、保存会话参数时，仍把内存里的旧设置提交到服务端、覆盖另一网页所做修改的问题；现在这些操作暂停提交，并显示「当前网页已暂停保存」一类的提示。
- 修复参考音频时长不在 3 到 10 秒时，扫描角色包仍提示成功、新角色不出现且不说明原因的问题；现在会列出不合格文件夹和中文原因，音色列表也能看到同一提示。
- 修复在 LOG 里连点「用当前音色重新生成」时，第二下被当成取消、界面却不说明的问题；开始生成后约半秒内再点同一按钮不会取消，主动点「取消生成」会说明本次已取消且原音频仍保留。
- 修复导入聊天备份的响应超时或丢失后，再次选择同一个文件会多插入一份相同会话的问题；现在同一份文件使用相同的会话编号，已经存在时整份都不再写入，并提示先刷新页面查看。
- 修复 LOG 里仍在有效期内的语音一旦加载失败就被标成「语音已过期或不可用」且不能再点的问题；现在只有解码失败或服务端确认文件不存在（404/410）才会禁用，网络中断、超时和其他暂时读不到的情况会提示稍后重试并保留播放按钮。

## [3.1.0] - 2026-10-24

### Changed
- 启动页改成首页：打开就回到上次聊天的场景，看到她和你们最后聊到的那句，可以接着聊、试听那句语音、切换到最近聊过的会话，或者换个角色、地点另开一段对话。右上角显示对话模型和语音引擎是否可用。第一次使用时会引导你连接模型、语音引擎和选择角色。首页上不再弹出音色切换的提示，进入对话时如果音色还没切换好会再切换一次。

### Fixed
- 切换音色时提示「语音模型尚未加载」，以及引擎目录检查通过后的提示，都让用户去「状态诊断」，可这个标签页早已改名为「运行状态」。现在两处都写「运行状态」。
- 在「对话模型」页填了 Key 或改了地址等设置后，点页脚的「保存全局配置」曾提示已保存，但模型设置其实没存。现在会一起保存并启用；保存失败时会说明原因。未改动模型表单时不会保存或切换当前模型。
- 修复切换会话后，读屏软件仍能读到上一个会话最后一句台词的问题。

## [3.0.0] - 2026-10-23

### Added
- ADV interface (P0–P8): line-by-line playback with voice, acting tags,
  layered sprites with blink and lip sync, LOG history and voice replay,
  settings, backgrounds by time of day, and a title screen with session loading.
  ADV is now the only UI, with touch controls, sprite/title fades that respect
  reduced motion, and background and
  queued-face preloading. Portrait and background assets stay local only.
- Background packs per work (`backgrounds/<work>/manifest.json`). A character
  package can name its `work`; the background picker lists that work first, and
  its default "自动" shows the work's default background.
- `scripts/dev_standins.py` starts a fake LLM and a fake GPT-SoVITS server (old,
  per-line and deliberately broken reply formats; WAVs with a speech-like envelope)
  so the chat, voice and playback pipeline runs without a GPU or a paid model.
- `docs/design/adv-ui.md`, the design spec for the ADV interface.
- "允许私网 LLM 端点" switch in chat model settings for Ollama, LM Studio and LAN
  models. It saves on change; testing or enabling a model waits for the save.
  Model lists and connection tests now apply the same private-network rule as chat.
- A reply that never starts leaves a note under the user's message with the reason,
  a "重新发送" button that retries the same message in place (no duplicate bubble,
  recordings kept) and, for model problems, a shortcut to the model settings.
- Chat errors carry a stable `error_code` and readable Chinese guidance: a missing
  API key names the provider, and a blocked private endpoint points to the switch
  above instead of being reported as a DNS failure. Connection tests explain a
  missing key the same way.
- The model status says "未填写 API Key" when the active cloud provider has no key,
  and "正在读取模型配置…" until the model list loads instead of briefly claiming that
  no model is configured.
- `scripts/verify_windows_portable.py` checks the release ZIP's checksum, required
  files, empty user folders, excluded secrets and models, and that its web files
  match the deployed and built frontend.

### Changed
- ADV: LOG no longer lists lines of the current reply that the stage has not
  shown yet, so opening it mid-reply does not reveal the next lines.
- The browser now asks for per-line replies with acting tags by default. Session
  settings → 回复格式 → "旧格式：整段回复" switches a session back. Sessions saved
  with the earlier default also move to the new format; API clients that omit
  `reply_format` still get the old format.
- Voice replies play by default in a browser that has no saved settings, so the
  first ADV conversation is voiced. A browser that already saved the voice switch
  keeps it; "语音开关" on the stage or 设置 → 自动播放语音 changes it.
- Sending a message goes through `frontend/src/conversation.js`, a DOM-free
  controller that emits turn, text, audio, done, error and cancelled events; the
  ADV stage subscribes to it. `main.js` starts through an injectable `initApp()`, and
  the history and model-badge tests import the real modules instead of slicing
  source text.
- Persona prompts without the bilingual reply format (new custom voices, edited or
  third-party packages) get the standard JSON format appended, so replies keep a
  Japanese line for the voice instead of having Chinese text read aloud.
- Toasts stay above the message input as it grows, and only their action button
  takes clicks, so retyping after an error no longer opens settings by accident.
- Portable builds explain updating by extracting a new package instead of offering
  a git update.
- The Docker image starts through `scripts/run_container.py`, which turns console
  authentication on unless `GALGAME2VOICE_AUTH_DISABLED` is set. The default moved
  out of the image's `ENV`, where its name failed Docker's build checks.
- The LLM endpoint check now treats every non-global address as private, including
  `100.64.0.0/10` (shared CGNAT space, which Tailscale uses) and IPv4 addresses
  carried in IPv4-mapped, 6to4 and Teredo IPv6. A model served over Tailscale
  needs 「允许私网 LLM 端点」 turned on.

### Fixed
- After a reply failed with a model error, such as a refused connection or a
  blocked private endpoint, 「去设置」 in the text box opened the session settings,
  which have no model fields. The text box and LOG now choose like the error
  toasts: model errors open 「全局设置 → 对话模型」 and a rate limit opens the
  session settings.
- A new session's greeting still described the old chat layout (a speaker in the
  top bar, a gear at the bottom of the sidebar) and showed raw `**` marks, since
  LOG and the title screen's 读取 slots print plain text. It now points to the
  text box, its microphone and the CONFIG panel. Backups still recognize the old
  greeting, so older sessions are not exported as extra browser copies.
- In a landscape window the title screen's menu ignored mouse clicks, which went
  to the hidden input box underneath; only the keyboard worked. Mouse clicks now open the item.
- Holding Ctrl to fast-forward until a reply ends in input mode no longer leaves
  a stale temporary-skip flag that later turns off a manually enabled `SKIP` when
  the window loses focus or when skip is toggled.
- `resolve_reference_audio_path()` and `validate_reference_audio()` now check
  `project_file()` before falling back to the current working directory for
  relative paths, preventing colliding files in the launching directory from
  overriding project reference audio.
- `find_ffmpeg()` now falls back to a bundled `imageio-ffmpeg` binary when no
  system `ffmpeg` is on `PATH`, so `doctor` and audio conversion work in virtual
  environments that have only the wheel installed.
- Cached TTS WAV files under `/audio/cache/` keep their `Cache-Control: public,
  max-age=31536000, immutable` header instead of having it overwritten with
  `private, max-age=0` by the general `/audio/` middleware rule; message deletion
  runs inside `immediate_transaction` for WAL savepoint safety.
- On the ADV stage, re-recording voice input revokes the previous recording's
  Blob URL and guards async `recorder.onstop` callbacks across resets; audio cache
  lookups refresh LRU order; suspended `AudioContext` instances resume on stage
  interaction or fall back without stalling AUTO mode; modal overlay clicks no
  longer bleed through to advance the stage; and SSE parsing handles `CRLF` line
  endings.
- After sending a message, Space, Enter and Esc work at once on the ADV stage.
  Sending hid the focused input box, focus fell to the page body, and the stage's
  shortcuts heard nothing until the stage was clicked; Esc could not stop a reply
  that was still generating.
- Two database backups taken within one tick of a coarse system clock (about 2 ms
  on some Windows machines) got the same name, so the second replaced the first.
  The second now waits for the next tick.
- Voiced sentences without their own emotion cue now show the reply's declared
  emotion on the portrait instead of always falling back to "gentle".
- Turning automatic voice playback off is saved immediately; a reload no longer
  turns it back on.
- Project-relative reference audio resolves against the install root, so a
  relocated data root or a launch from another working directory still sends an
  absolute path to GPT-SoVITS.
- Character rescans run one at a time; lookups keep the last complete catalog during
  a scan, and a failed scan leaves it intact. Rescanning keeps an unsaved persona
  draft and the selected model files.
- Loading older history waits for the current reply, and sending waits for the older
  history to finish loading.
- The Docker image creates the characters folder for its non-root user and compose
  mounts `./characters`; the app also starts when that folder cannot be created.
  The build context now contains only what the image copies, so character models
  and user data are no longer uploaded on every `docker build`.
- `doctor` probes GPT-SoVITS at `/control`, like the app's own health check. It
  no longer reports a running api_v2 server as unreachable.
- The one-click update no longer runs a local frontend build when the update
  already includes the built frontend. Installs without Node.js no longer see a
  build failure after a successful update.
- Long-term memory no longer mistakes everyday phrases such as "我叫了个外卖",
  "别叫我笨蛋" or "明天叫我起床" for the player's name. Before, the character
  started calling the player by that phrase, and the web UI cannot correct it.
- A new "我喜欢…" or "我讨厌…" about the same thing replaces the opposite memory
  instead of keeping both. The player's name and occupation are still recalled
  after more than 200 newer memories, and memory recency is computed in UTC.
- The relationship stage sent to the model now changes at the same scores as the
  level (20 and 80), so Lv.5 no longer comes with the third stage's guidance.
  Level-ups are judged from the score alone.
- The memory API answers 422 instead of 500 for a value that is empty after
  sanitizing, and no longer ignores such a value on update.
- Global settings can no longer be saved before they have loaded, and editing a
  field while they load no longer leaves the others at their defaults. Either
  case used to overwrite the saved settings, turning the Telegram bot off and
  resetting the engine address and precision. Restarting the engine before the
  settings load keeps its saved precision.
- When the voice list fails to load, saving session settings keeps the session's
  character instead of unbinding it. Deleting a voice, or finding a session's
  voice deleted, also clears the binding on the server, so it no longer returns
  after a reload.
- Characters created without a persona can save their session settings again.
  Saving session settings or deleting another voice no longer reloads the
  portrait, which reset its costume and last line.
- Clicking the "聊天记录备份与恢复" heading no longer opens the import file picker.
- After `/character`, Telegram replies use the new character's persona, memories
  and affection instead of those of the character the chat started with.
- In Telegram groups, `/nickname` is saved for the member who sent it, and one
  member's message, `/reset` or clear button no longer cancels another member's
  pending voice note.
- Telegram voice notes sent while the active provider has no speech recognition
  get a clear reply instead of a generic error, and a configured speech
  recognition model is used.
- Restarting the Telegram bot while it is still starting (for example by saving
  settings right after launch) no longer leaves a second bot polling.
- One slow model reply no longer holds up the Telegram bot for every other chat;
  each member's messages are still answered in order.
- A reply that does not follow the JSON format is shown, saved and voiced in full;
  before, only its first 16 or so characters remained. A short preamble before the
  JSON ("好的～") no longer puts raw JSON in the chat bubble instead of the reply,
  and the "日文" label no longer shows up at the end of a labeled reply.
- The chat bubble and its local history end with the reply as the server saved it.
- Stopping a reply, or a provider error during one, saves the partial reply once
  instead of twice; the copy was repeated in the next turn's context. Pressing Stop
  or closing the tab while a reply streams now keeps the partial reply, and removes
  a message that got no reply, instead of losing both with the cancelled request.
- An empty reply no longer earns affection. It withdraws the player's message, so
  sending the message again counted twice. The non-streaming chat API now awards
  affection only after the reply is saved.
- A reply with an empty "chinese" field is no longer shown, saved and voiced as
  raw JSON in the non-streaming API and Telegram; Telegram reports it as a failed
  reply.
- Audio error messages, including the one shown when the browser blocks playback,
  pointed to a per-message play button that the ADV interface no longer has. They
  now point to the 「语音」 and 「播放录音」 buttons in LOG.
- Voice engine messages still sent users to 「语音与推理」, the old name of the
  「语音服务」 settings tab, and to a 「重启引擎」 button and a "CPU 稳定模式" the
  page doesn't have. They now name 「语音服务」, its 「计算方式」 setting's
  「CPU 模式」, and 「重启 SoVITS 引擎」 in 「运行状态」.

### Removed
- Legacy `galgame2voice/static/js/audio_player.js` and `chat_client.js` scripts
  left over from the pre-ADV chat UI, along with the `js` preservation rule in
  `frontend/deploy.mjs` and tests targeting those unused files.
- The speech recognition engine selector (本地 SenseVoice / Whisper) in global
  settings. Those engines were never connected; voice input always used the
  browser's speech recognition.

### Security
- The Telegram bot applies its admin whitelist to every command and button, not
  only to chat messages. Before, anyone who found the bot could switch the active
  character (also for the web UI) and view the model settings and spending.
  Rejected users are told their Telegram user ID so the owner can whitelist it.

## [2.1.0] - 2026-10-14

### Added
- Hot-reload character package discovery endpoint (`POST /api/characters/rescan`)
  and a "扫描" button in session voice settings, allowing newly extracted
  character packages in `characters/` to be discovered and bound immediately
  without restarting the server.
- Cursor-based session history pagination (`GET /api/chat/history?before_id=...`
  with `has_more` and `oldest_id`), and an in-chat "↑ 查看更早的聊天记录" action
  that prepends older messages while preserving scroll position.
- Actionable recovery buttons on error/info toasts (one-click navigation to
  LLM model configuration, voice engine status, session voice settings, and
  chat backup settings), plus first-install setup chips in empty chat sessions
  and clickable header/diagnostic status badges that also show the session's
  bound character name.
- Full chat export (`GET /api/chat/export`) that reads one database snapshot and
  includes sessions and messages outside the sidebar/history limits. Browser-only
  text is retained separately when it differs; repeated messages remain distinct.
  Offline export is clearly labeled.
- Atomic chat import (`POST /api/chat/import`) preserving original session and
  message timestamps; oversized backups fail explicitly instead of truncating.
- Portrait layer exporter (`scripts/tools/compose_portrait_layers.py`) that
  preserves original per-layer coordinates and ordering in one shared costume
  frame, validates source resolution, and records source hashes.
- Verified SQLite snapshots including committed WAL data, daily startup backups,
  and a portable database recovery launcher (`--restore-database` / `数据恢复.bat`)
  that preserves original files.
- Actionable runtime recovery guidance, bounded settings requests, and engine
  readiness polling that distinguishes model loading from startup failure.
- Repeated portable launches reuse the running installation and its actual port.
- Local GPT-SoVITS directory selection and persistence in the settings console;
  sibling engine paths survive relocation and feed model scans and restarts.
- Portable startup exceptions leave a diagnostic log in `logs/launcher_error.log`.
- Windows x64 portable packaging with a frozen interpreter, locked application
  dependencies, frontend, FFmpeg and included third-party license files.
- `--no-engine` opens the application without managing a local voice engine.

### Changed
- Chat titles and model names use separate lines; message spacing, contrast,
  controls and portrait panels remain readable in narrow windows.
- Global settings use category navigation and concise descriptions. Session
  settings keep character controls visible and group advanced tuning in details.
- Dialogs preserve focus, support keyboard navigation and handle nested token
  prompts. Closed drawers and portraits are excluded from keyboard navigation.
- Portrait image URLs include file revisions so re-exported images invalidate
  browser and preload caches even when their names are unchanged.
- Developer documentation is grouped under `docs/development/`; local caches and
  archived scratch files stay under `.local/`, excluded from source releases,
  Docker contexts and model discovery. Added `使用说明.md` folder guide.

### Fixed
- Release packaging (`scripts/package_release.py`) now excludes archive files
  (`.zip`, `.7z`, `.rar`, `.tar`, `.gz`, `.xz`) and additional weight formats
  (`.pt`, `.gguf`) so user-downloaded character archives in `characters/` never
  bloat source release bundles.
- Session persona editor is disabled when no character is bound (except during
  new custom voice creation, where the typed persona is automatically saved with
  the new profile), preventing accidental unsaved edits.
- Session lists paginate beyond the first 50 entries. Saves for one session run
  in order; deletion waits for them and removes local records only after confirmation.
- Deleting or clearing chats leaves shared audio to the normal retention worker,
  protecting other conversations, reference files, and audio during transaction rollback.
- External data directories no longer leak absolute host paths in system status.
  History timestamps interpret SQLite dates as UTC, and failed history loads can be retried.
- Streaming text renders each increment immediately. The final message takes
  over live playback controls without restarting completed speech, losing
  pause state, or advancing portrait dialogue ahead of paused audio.
- Audio downloads, body reads, decoding and cache access have bounded waits.
  Failed chunks release the queue, surface a recoverable message and allow later
  speech to continue. Playback itself fills the cache without a duplicate fetch.
- Repeated sentences sharing a cache URL retain their separate playback slots.
  Pausing stays effective as new chunks arrive, and portrait cues use the audio
  clock rather than wall-clock timers. Blocked autoplay offers a click to resume.
- Late cache reads, translations and synthesis results cannot steal playback
  from a newer click or session. Clicking a loading message again cancels it;
  temporary connection failures preserve its audio references instead of
  triggering unnecessary synthesis. Only confirmed missing audio is regenerated.
- Configuration errors no longer replace diagnostic widgets. Refreshing after
  a failed request restores the status display and preserves unsaved fields.
- Explicitly clearing a session voice binding now removes the SQLite binding;
  omitted bindings retain their existing value, and empty settings can be saved.
- Voice creation binds to its originating session, stale selector responses cannot
  replace a newer session's options, and the active voice remains selectable.
- Voice, persona, Telegram, cache and session requests have deadlines without
  automatic write retries. Failed persona saves preserve the open editor and its
  text; failed session saves no longer claim confirmed persistence.
- Audio cleanup defers when reference or cache protection metadata is unavailable,
  preserves cache entries whose files cannot be safely removed, and moves expired
  cache disk operations off the event loop. Registered reference samples inside
  the cache also remain protected. Cleanup resumes after recovery.
- Late history loads cannot replace newly typed messages or revive a deleted
  session, and session switching renders immediately while history loads.
- Developer documentation is grouped under `docs/development`; local caches and
  archived scratch files stay under `.local`, excluded from source releases,
  Docker contexts and model discovery. A short Chinese folder guide identifies
  the launch entry point, user data and development files.
- Restored schema version 7 compatibility after an upstream sync dropped the
  legacy foreign-key migration. Existing databases keep their version and data;
  older character cascades are rebuilt transactionally without losing indexes,
  triggers or sequence counters, and unknown future schemas remain blocked.
- Portraits support existing emotion-named PNG packs as well as numbered
  differential indexes, without rewriting installed character assets.
- The portrait stage follows the current session even when voice switching
  fails or the engine is offline. Stale metadata, images and stream completions
  cannot replace the newly selected character; costume fallbacks stay in the
  selected outfit, and unavailable portraits offer explicit recovery guidance.
- Dialogue cards receive reply text instead of message objects. Frontend HTML
  uses LF consistently so Windows builds match the committed web artifacts.
- Interrupted chat streams preserve partial replies and report incomplete
  responses; cancellation releases readers and does not resend generation.
- Voice switching respects memory protection instead of automatically forcing
  a second attempt after a 503; failed saves and refreshes preserve input.
- Engine restarts only stop owned processes, serialize operations through browser
  disconnects, and validate the installation before changing configuration.
- Windows job APIs preserve 64-bit handles; shutdown closes the job and only
  terminates processes owned by the launcher, without trusting stale PID files.
- Native file pickers serialize Tk creation and cleanup on their owning thread.
- Saving an engine directory invalidates model discovery without allowing an
  older in-flight scan to repopulate the cache; pending settings reads preserve edits.
- Credential encryption now declares a locked `cryptography` dependency and
  emits only AES-GCM/DPAPI ciphertext. Legacy stream ciphertext is read only for
  migration; unreadable, damaged or unpersisted master keys fail explicitly.
- Docker uses the Settings-based entry point with authentication enabled by
  default, so its listener and network-exposure check agree.
- Fresh databases start without machine-specific voice profiles. Session voice
  settings guide first-time setup, while existing profiles are retained.
- Dynamic CRUD updates validate explicit SQL column allowlists. Directory
  browsing stays within authorized roots, and active-profile persistence errors
  reach callers instead of reporting a successful switch.
- Tests isolate credential, database, audio, log and temporary files and reset
  runtime singletons. Audio tests configure their own neutral voices; native-picker
  tests skip when tkinter is unavailable. CI compares deployed frontend artifacts
  with tracked files; blocking file operations are offloaded and ASYNC240 is enabled.
- TTS cancellation now stops stalled synthesis jobs without stopping the priority
  worker, and reaps enqueue helper tasks when audio buffers are full.
- Scheduler shutdown cancels queued jobs, wakes stream consumers, and drains the
  queue before the shared GPT-SoVITS HTTP pool closes.
- Cancelled upstream streams no longer block while writing errors or EOF into an
  abandoned full buffer; service streams close their nested generators explicitly.
- Interrupted audio is excluded from the persistent cache. Single-flight and
  stream errors still reach callers without deferred unhandled-future reports.
- Queued bytes/file synthesis requests recheck the shared cache before inference;
  a regression burst of 16 identical requests now invokes the mock engine once
  instead of 16 times, including mixed bytes/file callers.
- Cache keys resolve speed/temperature aliases and quality presets using the
  engine's option parser, and include explicitly requested voice profile IDs.
  Versioned keys prevent reusing ambiguous older entries while retaining their
  audio files for conversation-history playback.
- Streams with caching disabled no longer retain every received audio chunk.
- Lifespan cleanup also runs when the serving context raises. Startup tests use
  isolated settings and mocked voice clients, and batch entry-point tests execute
  a stub launcher to check paths and arguments containing spaces and `!`.
- Non-repository version tests bound Git's parent-directory search, allowing
  temporary test directories located inside a development checkout.
- Cached file responses now read only file metadata; cached streaming starts
  with a bounded chunk rather than loading the whole clip and records one hit
  per playback. Closing the service stream immediately closes its disk reader.
- Audio clips exceeding the RAM budget remain on disk without displacing small
  hot entries. Disk streams also stop buffering if a file grows past the budget.
- File cache lookups detect deleted or empty files even with a warm RAM entry.
  Concurrent overwrites read previous sizes after write admission, keeping disk
  capacity counters consistent and avoiding premature pruning.

## [2.0.0] - 2026-10-06

First tagged release. The single source of truth for the version is
`pyproject.toml` (build) / `galgame2voice.__version__` (runtime).

### Added
- `CHANGELOG.md`: release notes are now maintained per release.
- GitHub Release workflow (`.github/workflows/release.yml`): pushing a `v*`
  tag runs lint, the full test suite, rebuilds the frontend bundle, builds the
  release ZIP via `scripts/package_release.py`, and uploads it as a release
  artifact. The workflow also guards against new hardcoded `127.0.0.1:9880`
  literals outside the unified endpoint resolver.
- `galgame2voice/services/sovits_endpoint.py`: single unified resolver for the
  GPT-SoVITS engine address. Priority is now explicit and shared by every
  component: process environment
  (`GALGAME2VOICE_GPT_SOVITS_BASE_URL` / `GPT_SOVITS_BASE_URL`) > SQLite
  `gpt_sovits_url` (Web console) > `.env` > built-in default
  `http://127.0.0.1:9880`. Only loopback endpoints are managed as local
  processes; remote endpoints are never started, killed, or restarted by this
  host (`POST /api/system/restart_sovits` answers 409
  `REMOTE_SOVITS_NOT_MANAGED` for remote endpoints).
- Release packaging (`scripts/package_release.py`) now excludes credential and
  backup material from release ZIPs: `data/.master_key`, `data/.console_token`
  and the whole `data/backups/` tree are never shipped.

### Changed
- Version metadata unified at 2.0.0: `frontend/package.json` aligned from
  `0.1.0` to `2.0.0`; `docker-compose.yml` image tag pinned from `:latest` to
  `:2.0.0`; `scripts/package_release.py` reads the version from
  `pyproject.toml` instead of hardcoding it; `galgame2voice/config.py`
  `app_version` and the health-endpoint response schemas now default to
  `galgame2voice.__version__` instead of repeating the literal.
- `POST /api/system/restart_sovits` now restarts the *configured* endpoint
  (host/port from the unified resolver) instead of always spawning
  `127.0.0.1:9880`; it waits for the old process's port to be released
  (bounded polling, 409 `SOVITS_PORT_STILL_IN_USE` on timeout) instead of a
  fixed 1-second sleep.
- Launcher (`scripts/run_server.py`) no longer falls back to a local
  `127.0.0.1:9880` when the configured endpoint is unreachable, and no longer
  mistakes an unrelated listener on local 9880 for the configured engine. A
  remote endpoint that is down produces a clear warning instead of silently
  starting a local engine the app would never use.
- Saving `gpt_sovits_url` in the Web console re-resolves the effective
  endpoint before hot-applying it, so an explicit process environment variable
  keeps precedence instead of being silently overridden by the just-saved DB
  value.
- Rebuilt the committed frontend bundle (`galgame2voice/static/`) from
  `frontend/` via `npm run deploy`; the bundle is a build artifact again
  instead of a stale copy.

### Fixed
- Dashboard cache: the frontend reads `total_entries` but the backend only
  exposed `total_files`; the backend now returns the compatible field.
- `ChatService.stream_chat_events` now forwards `voice_profile_id` to
  `stream_chat` instead of dropping it.
- Empty/missing admin allowlist in `check_is_admin` is now fail-closed
  (consistent with `_is_admin`) instead of fail-open.
- FP16→FP32 auto-restart no longer drops the `device=` argument when
  respawning the GPT-SoVITS process.
- Release ZIPs no longer ship `data/.master_key`, `data/.console_token`, or
  `data/backups/` contents.
- ~170 code-health polish rounds corrected factually inaccurate prose
  (comments, docstrings, docs): false performance/efficiency claims, stale
  `(PLANNED)` markers for shipped features, absolute robustness/completeness
  claims, and inaccurate timeout/threshold values, among others. No behavior,
  API, or control flow was changed by these rounds.

### Known issues
- The committed `galgame2voice/static/` bundle is regenerated with
  `npm run deploy`; do not hand-edit it.
- 44 pre-existing test failures in the Linux environment (missing character
  assets, no network/GPU) are unrelated to this release; the failure set is
  byte-identical before and after these changes.

[3.1.2]: https://github.com/LevenST24/galgame2voice/releases/tag/v3.1.2
[3.1.1]: https://github.com/LevenST24/galgame2voice/releases/tag/v3.1.1
[3.1.0]: https://github.com/LevenST24/galgame2voice/releases/tag/v3.1.0
[3.0.0]: https://github.com/LevenST24/galgame2voice/releases/tag/v3.0.0
[2.0.0]: https://github.com/LevenST24/galgame2voice/releases/tag/v2.0.0

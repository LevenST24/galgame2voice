# Changelog

All notable changes to galgame2voice are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- 完成全局设置控制器业务职责与生命周期解耦（T04 收口）：将遥测轮询与硬件卡片渲染、GPT-SoVITS 引擎精度/重启/缓存清理，以及 Telegram 开关/可见性/验证与测试逻辑分别抽离至独立的 `TelemetrySettingsController`（`telemetry_settings.js`）、`EngineSettingsController`（`engine_settings.js`）与 `TelegramSettingsController`（`telegram_settings.js`）；`global_settings.js` 保持公共兼容门面与配置草稿协调；每个控制器实例独立拥有 AbortController、代数守卫、定时器与事件解绑；`initGlobalSettings` 在重新初始化时显式释放旧控制器与旧监听器，消除了旧请求回写新 DOM、覆盖忙碌状态、旧重启阻塞新操作或重复绑定导致多次调用的缺陷。
- 拆分设置控制器第一阶段（Phase 1，PR294）：将大模型提供商表单、参数草稿与读取状态、模型与密钥展示、连通性测试及保存启用逻辑从 `global_settings.js` 抽离至实例拥有的 `ProviderSettingsController`（`provider_settings.js`），保持原有页脚保存预约顺序、版本守卫与草稿隔离语义。

### Fixed

- 修复旧 Telegram 连通性测试在途时重新打开设置或发起新测试后，旧响应结束时错误提前解锁新测试按钮并弹出旧提示的问题，以及测试进行中编辑输入框会导致测试按钮永久锁死的问题；现在 Telegram 测试拥有独立的代数守卫与 AbortController，草稿变更或重新初始化立即作废旧测试且不泄露提示或干扰新状态，编辑输入框会即时重置忙碌态以允许再次重试。
- 修复在 Telegram 对话中模型输出达到最大生成长度限制时，仅发出半截回复且没有任何提示的问题；现在半截回复后会追加与网页端一致的提示，建议调大「最大生成长度」或降低「思考强度」。
- 修复修改角色包的人设时，若保存到数据库失败且回滚角色包文件也失败，错误提示仍声称「已回滚角色包修改」的问题；现在回滚失败时如实提示「角色包文件未能回滚」，回滚成功时提示保持不变。
- 修复在磁盘空间已满时保存角色人设，原错误提示被次生错误覆盖导致界面仅显示笼统的 500 内部错误且无法得知具体原因的问题；现在会正常提示角色配置写入失败与磁盘空间不足的明确说明。
- 修复在全新安装或存量数据库启动时会改写用户已保存模型名称、Telegram 控制台模型切换菜单缺少 SiliconFlow 与 Moonshot，以及清空提供商模型名称时错误按 OpenAI 模型请求导致对话失败的问题；现在启动时自动补齐缺失的内置提供商并完整保留用户保存的模型名称，清空模型名称时自动按当前提供商预设默认模型发起对话，切换未初始化的服务商时自动补齐并保持正常激活状态。
- 修复接入使用 `<thinking>` 思考标签的模型（如经兼容网关接入的 Nova 等）时，思考内容被误当作台词朗读并显示在实时字幕与 Telegram 消息中的问题；现在思考内容会被自动剥离，仅朗读与展示正式回复。
- 修复在 Telegram 对话中模型回复被服务商内容安全过滤截断时，仅发出半截回复且没有任何提示的问题；现在会在回复后追加内容被过滤的中文提示，正常结束时不追加。
- 修复系统环境变量已配置其他 FFmpeg 时，便携版启动自检报错崩溃或误用不兼容外部音频组件的问题；现在优先使用便携包与项目内置的 FFmpeg 组件，仅在未内置时回退至系统环境。
- 修复模型回答触发服务商内容安全策略被中断或拦截时，界面仅当作正常完成或报通用空回复且未提示安全过滤原因，以及同步对话在回复被截断时未标记截断状态的问题；现在触发安全策略时会明确提示内容被安全过滤拦截，同步对话在回复截断时也会如实标记截断状态。
- 修复在 Telegram 中通过指令或控制台切换角色时，若语音引擎在线但目标角色模型加载失败，界面仍误报语音引擎离线并强行切换人设、导致人设与实际发音错位的问题；现在会如实提示模型加载失败并保留原角色不变。
- 修复模型服务返回余额不足（HTTP 402）、上下文超长（HTTP 400）、部分模型参数错误或反向代理返回 502/524 网页时，错误提示误报为 API 密钥失效且包含原始网页代码的问题；现在能准确提示余额耗尽、上下文过长或网关超时并提供针对性处理指引，不再误报密钥错误或泄露网页源码。
- 修复接入推理模型（如 DeepSeek-R1、QwQ 等）对话时，模型输出的思考标签及内容会导致误报空回复、思考过程泄露到字幕与 Telegram 消息以及被送入语音朗读的问题；现在会自动过滤思考过程仅展示与朗读正式台词，并在思考内容占满生成长度导致截断时明确提示长度耗尽。
- 修复使用 `--fp32` 或 `--cpu` 参数启动服务后，在网页设置中心修改计算方式或重启语音引擎仍跳回启动时的旧模式、状态卡片一直提示待重启，以及 `--cpu` 启动时控制台日志误显示为 `FP32` 的问题；现在网页端修改计算方式和重启引擎会按新选择的模式生效，`--cpu` 启动日志正常显示为 `CPU`。
- 修复未安装预置角色包或仅配置了自定义音色时，启动器精度校准无法找到参考音频导致静音显卡（如 GTX 1650/1660 系列）无法自动切回单精度并始终静音的问题；现在会自动选用已有的角色包或自定义音色参考音频完成校准，确保静音显卡正常发声。
- 修复自动识别同级放置的 GPT-SoVITS 语音引擎后，将整合包整体移动或复制到新位置时，引擎连接失效或误连旧目录的问题；现在同级引擎按相对位置记录，整体移动或复制整合包后仍可自动识别并正常连接新位置的引擎。
- 修复角色台词仅包含动作描写与省略号或语气标点（如「（ため息）……」）时，流式语音合成报错且在历史记录中点击重新生成语音提示失败的问题；现在此类纯动作与标点台词会正确跳过语音合成，不再产生错误音频状态。
- 修复在 Telegram 对话中角色只回复动作或神态描写、没有可朗读的日文台词时，顶栏仍显示“正在录制语音”且后台语音合成报错的问题；现在这类回复只发送文字消息，不再显示录音状态或触发语音合成。
- 修复在生成多条对话语音后新建自定义音色时，可选参考音频列表被临时语音文件占满导致用户放入的参考音频无法显示的问题，以及名称包含 cache 的用户文件夹被误忽略的问题；现在模型扫描自动跳过临时语音文件并仅排除系统自身的缓存目录，正常显示用户的参考音频。
- 修复角色包配置了自定义默认情绪时，默认语音参考音频及未知情绪仍错误选用温柔情绪（或列表中首个情绪）导致语气不符的问题；现在会优先使用角色包指定的默认情绪。
- 修复模型服务返回鉴权失败、模型不存在或请求参数超长等 4xx 错误时，若报错内容包含特定数字（如字数统计、请求编号）或单词片段，被误判为网络连接中断并错误提示“无需修改 API Key 或模型名称”的问题；现在这类报错会如实给出对应的鉴权、模型名称或请求错误提示。
- 修复使用命令行参数 `--cpu`、`--fp32`、`--fp16` 或 `--precision` 启动服务后，带独显设备上的运行状态卡片误报需要重启且在网页点击重启会丢失命令行指定模式的问题；现在状态卡片如实显示当前计算模式，网页端重启引擎也会继续沿用命令行指定的计算模式与自动判定优先级。
- 修复启动器拉起受管语音引擎后，若在设置中将服务地址配置为 `http://localhost:9880` 等本机回环别名，导致状态卡片精度显示为“待确认”、引擎异常退出时丢失崩溃诊断日志且在网页点击重启提示端口已被占用无法重启的问题；现在已支持识别本机回环别名，可正确识别受管引擎状态并在网页正常重启。
- 修复计算方式处于默认「自动判定」时，在设置中心点击「重启 SoVITS 引擎」会丢失启动时的显卡精度校准结果、导致已校准为 FP32 的显卡重启后退回静音 FP16 的问题；现在保持自动模式重启引擎时会沿用已校准的精度，仅在从手动模式切回自动时才恢复默认自动判定。
- 修复选用 Anthropic Claude 模型时连接测试通过、但发起对话因同时发送温度与采样率参数而报 400 错误无法回复的问题；现在指定温度时不再同时发送采样率参数，对话可正常回复。
- 修复在 Telegram 伴侣控制台调整语速、发音温度等语音参数后发起对话时，语音合成未按所设参数生效而是回退为模型默认语速与温度的问题；现在对话语音始终按控制台保存的语音参数合成，同时正常匹配情感语气。
- 修复对话请求或流式回复中途因网络波动、代理切换节点等导致连接被重置断开时，错误提示误导用户检查 API Key 或模型名称的问题；现在会提示连接中途断开，引导检查网络与代理稳定性后直接重试。
- 修复保存角色人设途中若请求被中断或服务重启，未保存成功的人设草稿仍残留进角色包并在下次启动或重新扫描角色包后覆盖原有人设的问题；现在保存中断时会恢复修改前的人设。
- 修复删除当前选中的音色或仅剩的最后一个音色后，全局设置仍记录已删除的音色且后台仍按该音色发起语音预热的问题；现在删除后会同步清空当前音色记录并跳过已删音色的预热。
- 修复 Telegram 对话收到文字回复后、语音还没发出来时切换角色，刚才那条回复会用新角色的声音读出来的问题；现在每条回复的语音都使用发出这条回复时的角色声音。
- 修复角色包立绘文件名或服装文件夹名包含 `%20`、`%2B` 等百分号编码字符时，角色立绘加载报 403 无法显示的问题；现在这类立绘可正常显示。
- 修复计算方式处于默认「自动」时，在设置中心保存其他全局设置会丢失启动时的显卡校准结果、导致运行状态误报待重启切换为 FP16 且重启引擎后退回未校准 FP16 的问题；现在原本就是自动模式时保存设置会保留已有校准结果，仅从手动模式切回自动时才重新校准。
- 修复选用 Anthropic Claude 但未填写 API Key 时，发起对话或测试连接仍会反复尝试联网并在连接超时后误报为网络超时的问题；现在会立即提示前往设置中心填写密钥。
- 修复尚未导入角色包且未配置音色时，生成语音会误提示去「设置中心 → 运行状态」检查引擎或切换 CPU 模式的问题；现在会明确提示前往「设置中心 → 角色与会话」导入角色包或创建自定义音色。
- 修复在程序运行期间向 `characters/` 放入新角色包后，在 Telegram 伴侣中心点击「🔄 刷新角色」看不到新角色的问题；现在点击后会重新扫描角色目录并显示最新角色列表。
- 修复角色包里用相对路径写的情感音频每解析一次，都会把项目上一级目录和 C:–F: 各盘根目录扫一遍找 GPT-SoVITS 的问题；这会拖慢启动和重新扫描角色包，有休眠硬盘时更明显。
- 修复对话生成的语音缓存超过 50 条后，新建自定义音色的参考音频列表被缓存文件占满、找不到自己放的参考音频的问题。
- 修复在设置中心保存了 Telegram Token 或语音服务地址后，`galgame2voice doctor` 仍报未配置或去检查默认地址的问题。
- 修复重启本机语音引擎在停止旧进程后若因端口未释放或启动前报错中止，运行状态会误报“启动失败：语音引擎在加载完成前退出”的问题。
- 修复程序所在路径包含 `#` 或 `%` 时，启动时读不到已保存的 GPT-SoVITS 地址、在上一级目录留下名字被截断的空文件，以及 `galgame2voice doctor` 把正常的数据库报成损坏的问题。
- 修复 GPT-SoVITS 装在 `C:`–`F:` 盘根目录或项目上一级目录（含一层同名嵌套目录）时，模型扫描列出的模型和参考音频在创建自定义音色或浏览目录时被拒绝的问题。
- 修复修改角色人设时数据库写入失败（例如数据库被占用），界面报错但角色包里的人设其实已被改掉的问题；现在写库失败会把角色包恢复原样。
- 修复角色的 `.pth` 模型放在 `data/` 等目录、文件名和所在文件夹里没有 siki/sovits/weight 字样时，新建自定义音色的 SoVITS 模型列表里找不到它的问题；GPT-SoVITS 自带的底模目录不会被列出。
- 修复在 Telegram `/console` 中修改语速、温度、切分方式等语音参数后，Telegram 对话的语音仍按默认参数合成，以及保存失败时仍提示已保存的问题。
- 修复开启隐私模式（`GALGAME2VOICE_PRIVACY_MODE=true`）后对话语音没有声音的问题；现在隐私模式下语音写成临时文件供本次播放，不写入语音缓存。
- 修复删除音色后，服务器上仍记着该音色的会话（例如删除音色的网页里没有这个会话，或解除绑定没能保存）在下次同步时又被绑回已删除的音色、之后发消息时语音报错的问题；服务器返回会话列表时改以数据库里的音色绑定为准。
- 修复聊天合成时自动加载了会话所选音色之后，在设置或 Telegram 里再次选择同一音色仍被当作未加载的问题：以前会重新执行切换和内存/显存检查，Telegram 也不会提示“已经是当前音色”；现在该音色会被识别为已加载，直接跳过。
- 修复角色包文件夹名超过 100 字符且 manifest.json 损坏时，扫描角色包和获取角色列表直接报 500 崩溃的问题；现在会截断兜底标识继续完成扫描，正常角色包照常加载并将损坏包作为不合格角色包提示。
- 修复 Telegram 控制台菜单点击已删除或不存在的音色按钮时，被误判为语音引擎离线、回复已切换并将悬空 ID 写入当前音色设置的问题；现在切换前会校验音色是否存在，音色不存在时提示已被删除并重新展示音色列表，且不写入设置。
- 修复 FP16 推理输出静音的显卡上，启动校准改用 FP32 重启引擎后立刻探测、因新引擎还没监听端口而误报「FP32 探针仍为静音或未完成」且不记住 FP32（每次启动都要重新校准）的问题；现在会像首次启动一样等新引擎就绪后再探测，新进程提前退出则按重启失败提示。
- 修复 GPT-SoVITS 引擎连上后不再返回时，非流式合成（`/api/voice/synthesize`、Telegram 语音）和切换音色时设置参考音频的请求一直等待、占着合成锁让后面的语音请求全部排队的问题；现在这些请求沿用 TTS 超时（单次读取最长 300 秒），超时后报错并释放合成锁。
- 修复设置重新初始化后，旧引擎重启阻挡新操作、已释放实例排队的重启仍会发出请求的问题；重启状态现在归属于各控制器实例，尚未发送的旧操作会跳过，后续配置保存仍按原顺序执行；退出旧状态等待不会取消已经发送给服务端的重启，也不会自动重发请求。
- 修复已退役全局配置 GET 请求在迟到返回时覆盖新协调器视图的问题；现在通过实例代数与 DOM 归属双重校验，保证旧配置读取不污染新初始化的界面。
- 修复 `waitForEngine` 就绪等待过程中在 `onProgress` 回调触发取消时仍解析为就绪成功的问题；现在回调后及定时器等待入口均立即响应取消信号并抛出 `AbortError`。
- 修复在相同 DOM 控件上重复调用 `initGlobalSettings` 会堆叠状态刷新、精度自动保存、Tab 切换及引擎目录监听器的问题；现在各控制器与协调器统一维护事件注销，`initSettingsTabs` 与 `initEngineDirectory` 返回清理接口供重新初始化时完整释放。
- 修复在相同 DOM 控件上重新初始化设置时，前序控制器释放未同步重置忙碌控件状态，导致 Telegram 测试、引擎重启、清理缓存及目录选择与保存按钮继承旧禁用状态的问题；现在各控制器在 `dispose()` 时同步释放自身持有的忙碌控件与按钮文本，退役请求的 `finally` 逻辑严格受 `disposed` 守卫保护，保证旧请求结束后不解锁新实例中正在进行的同类操作。
- 补齐旧版未分句助手历史回复（数据库中 `lines_json IS NULL`）的显式单句语音恢复流程：后端单句语音修复接口（`POST /api/voice/regenerate-line`）支持对 `lines_json IS NULL` 的旧版消息校验会话、消息编号与日文台词一致性后派生规范第 0 句，在合成前与入库前严格校验 `ReplyLineRecord` 规格（超长内容安全返回 422 且保留原音频与文本），通过事务前全量快照与 `immediate_transaction` 内的 CAS 校验防止并发覆盖，成功后原子写入规范 `lines_json` 并清空旧音频路径，严格区分 NULL、损坏 JSON（返回 422 且不修改原记录）与空数组（返回 404）；前端恢复控制器与 LOG 界面复用 600 秒显式生成预算与取消守卫，对显式空数组 `lines: []` 保留文本与音频但隐藏不可达的修复操作（不扩大控制器适用边界），兼容仅含 chunk/URL 结构的历史旧音频播放，通过真实音频控制器调度使确认丢失音频（404/410）安全进入失效状态并显式展示修复操作（不静默调用 16 秒未持久化合成），对临时网络抖动与超时保持重试能力而不误禁用，在旧版音频过期（>7天）或失效时提供当前音色重新生成，成功后原子就地安装规范第 0 句并更新展平播放元数据，即时允许在 LOG 中点击播放，在历史分页、聊天备份导出及重新连接时均可正常播放，并支持 409 只读远端同步；真实 GPU 与听感效果保持开放。
- 修复两个网页同时或先后重新生成同一句已持久化的助手台词语音时，后提交的页面收到 409 冲突后直接报错、重新打开 LOG 仍显示旧失败音频且无法同步另一页已修好语音的问题；现在收到 409 时只读拉取一次服务端最新消息历史，严格校验会话、消息编号与角色、台词文本、所选音色及归属守卫后更新本地台词与播放元数据并保存状态，在 LOG 明确提示已在另一网页修好并直接允许播放，且不重复发起合成请求。
- 修复在 ADV 背景选择对话框中按 Tab 或 Shift+Tab 时因 querySelectorAll 返回 NodeList 缺少 filter 方法而抛出 TypeError、导致焦点循环失效并逃出对话框的问题；现在先将节点列表转为数组再筛选焦点元素，按键时焦点在对话框内首尾控件间正常循环。
- 修复加载或导出聊天设置时，清洗全局配置误将缺失语言键判定为相同、导致全局设置与备份文件多出会话级 `advSubtitleLanguage` 的问题；现在仅在配置默认值包含主副语言项时修正冲突，全局设置与备份不再混入该字段。
- 修复欢迎语等不配音的台词在 LOG 里仍显示语音和重新生成按钮的问题：这些按钮对不配音的台词只会显示为不可用（“无语音”“这句缺少可配音的日文台词”），现在不再显示。
- 修复设置中心残留旧版气泡布局「日文原文显示」死配置且界面无对应操作按钮、用户修改后无任何效果的问题；现在移除该无效选项与冗余代码，并保证旧数据正常加载。
- 修复音色切换、音色创建、角色包扫描、音色删除与检查更新失败时，错误提示直接拼接英文异常原文且未指明下一步的问题；现在原始异常仅记录至日志，提示统一采用三段式中文说明发生了什么、产生的影响以及下一步操作指引。
- 修复 LOG 中等待单句语音重新生成时，点击其他条目的「语音」或「播放录音」会静默取消本次生成的问题；现在回放仅停止正在播放的音频，不影响在途的单句语音生成。
- 修复在全局设置中更改「计算方式」并自动保存后，该项仍被当作未保存修改的问题：之后在另一个网页里改了计算方式，再打开设置仍显示旧值，点「保存全局配置」还会把旧值写回；现在自动保存成功后，打开设置会显示服务器上的当前值。
- 修复早期创建或导出的会话仍显示旧版欢迎语、引导用户去已不存在的「CONFIG 面板底部」或「顶栏喇叭」的问题；现在加载或导入会话时会自动将完全匹配的旧欢迎语刷新为现行说明，真实对话消息与备份往返不受影响。
- 统一界面用词和标点（“会话”“运行状态”“返回首页”等），错误提示改为说明下一步怎么做。
- 修复用户提问在模型首字返回前主动取消对话（如切换会话或切换角色）时，服务端已清理孤立提问但前端既未标记失败也未清除服务端编号、导致 LOG 产生无法重试的无回复孤立记录的问题；现在取消且未收到回复时会清除已失效的服务端消息编号并标记为中断未收到回复，允许在 LOG 中重新发送。
- 修复单句台词的语音只合成了一部分时，LOG 里既不提示也不显示「用当前音色重新生成」按钮的问题；现在会提示「部分语音未生成，可重新生成」并可重新生成这一句。
- 修复打开「角色与会话」后、音色列表加载完成前点保存，会清空会话绑定的角色，或误报「人设草稿与当前角色不一致」而拒绝保存的问题。
- 修复在「设置中心 → 语音服务」改了本机引擎目录后直接点「保存全局配置」，引擎目录没有保存却提示已保存的问题；现在会一并保存引擎目录，保存失败时保留输入并显示原因。
- 修复检查出新版本后，重新打开设置中心或拉取更新失败时，更新状态被刷回「已是最新」的问题；以及点「检查更新」请求失败时没有错误提示、还留着旧的「当前已是最新版本」横幅的问题。
- 修复重启语音引擎报错后，运行状态卡片上的「启动失败 / 待确认」和原因马上被刷成「离线」或「在线」，以及引擎启动期间卡片偶尔闪成「离线」的问题。
- 修复切换音色失败（例如语音引擎离线）后，「角色与会话」仍把这个音色标成「当前加载」并拦着不让删除，以及只剩一个音色时无法删除它的问题。
- 修复浏览过很多历史会话后浏览器本地存储写满、当前会话的新消息和设置无法再保存到本地的问题；现在存储写满时会从最早更新的会话开始释放未打开会话的本地消息缓存（有发送失败待重试提问的会话除外），切回时从服务端重新加载。
- 修复从服务端同步来、还没打开过的历史会话在「会话」抽屉列表里都显示「暂无消息」的问题；现在显示该会话的最后一句。
- 修复长时间不刷新网页时，浏览器里缓存的语音只在打开页面时清理一次、之后一直累积占用磁盘的问题；现在每次缓存新语音后都只保留最近 100 条，自己的录音不会被删除。
- 修复首页「全部会话」页每 5 秒刷新一次时整页重建，用键盘选中的会话失去焦点、缩略图反复重新加载的问题；现在会话没有变化时只更新显示的时间。
- 修复尚未导入角色包且会话未绑定音色时，打开「角色与会话」修改温度、最大 Token 等常规参数无法保存（提示先完成新音色创建）的问题；现在未填写新音色内容时可直接保存会话参数，已填写新音色草稿时仍提示先创建并保留已填内容。
- 修复在聊天输入框粘贴或输入超过 4000 字发送时草稿被清空、且报错被误提示为「配置不完整或数值超出范围，请检查填写内容后重新保存。」的问题；现在输入框限制最多 4000 字，超出上限时保留输入框草稿并提示删减后再发送。
- 修复在设置中心、体验面板或访问令牌弹窗的输入框中用中文/日文输入法打字时，按 Esc 取消候选词会直接关闭弹窗或还原未保存预览的问题；现在输入法组字期间按 Esc 或回车只作用于输入法，不会关闭弹窗或误提交。
- 修复发送提问后在角色开始回复前刷新或重开网页，该提问在 LOG 里不显示未收到回复也无法点击「重新发送」，且可能漏同步其他页面新消息的问题；现在重新打开网页后，这类未收到回复的提问会标记为中断并支持重新发送。
- 修复首页正在试听台词时打开设置中心，试听语音没有停止、仍在弹窗后继续播放的问题；现在打开设置时会立即停止首页试听并恢复试听按钮。
- 修复网页端会话处于“跟随后端当前音色”（未绑定具体音色）时，在 Telegram 切换后端当前角色后发送消息或切换回该会话，舞台角色名字与立绘仍停留在旧角色的问题；现在发起对话或切换到未绑定会话时会自动同步后端当前音色并更新舞台角色名字与立绘，且不会打断正在进行的对话回合。
- 修复开启“自动调节语速与语气”时，对话语音与单句重生成仍被锁定为下方固定滑块的语速和语气、无法随台词情感与标点自动变化的问题；现在自动模式下会按对话内容动态调节语速与语气，仅在切换为固定模式时使用滑块参数。
- 修复在 Telegram 发送 `/reset` 或在伴侣控制台清空对话后，刷新网页或切换回该会话时仍显示已清空的旧消息、导出完整聊天备份时还会多出一份「（浏览器副本）」的问题；现在清空对话后网页端会同步清除已删历史，同时保留新对话欢迎语和待重发的失败提问。
- 修复在对话记录（LOG）中重新生成某句语音并点击「语音」播放后，继续重新生成其他句或加载更早记录时，已播过的那句再次出现“语音已生成，点语音播放。”提示的问题；现在语音播完或切播其他句后不再重复显示该提示，若浏览器拦截未播出语音则保留提示。
- 修复在设置中心的「运行状态」、「对话模型」、「Telegram」等非「语音服务」页面底栏仍显示「恢复默认」按钮，点击确认后当前页面没有任何变化、却会在后续保存全局配置时意外将计算方式与音频缓存保留时间改回默认值的问题；现在「恢复默认」按钮仅在「语音服务」页面显示。
- 修复历史消息超过 100 条的长会话中，发送提问后在角色开始回复前刷新或重开网页，该提问在同步历史后直接消失或缺少「重新发送」按钮的问题；现在这类未收到回复的提问会保留在会话末尾、标记为中断并支持重新发送。
- 修复在「跟随后端当前音色」（未绑定指定角色）的对话中，若角色包目录存在无效角色包告警，每发送一条消息都会重复弹出同一条红色告警提示的问题；现在对话过程中同一条目录告警只提示一次，仅在告警内容变化或打开角色设置、手动重新扫描角色包时再次提示。
- 修复从当前音色切换到新音色失败后删除该失败音色时，当前音色的「当前加载」标记和未绑定会话的舞台角色一直为空的问题；现在删除失败音色或重新确认当前有效音色后，会自动恢复当前音色的加载标记与舞台角色显示。
- 修复无独立显卡的设备在设置中心「运行状态」中误显示「切为 GPU(FP16)」快捷按钮、点击后导致语音引擎启动失败的问题；现在纯 CPU 模式下会自动隐藏该切换按钮并提示当前仅支持 CPU 模式，若异常处于 GPU 模式则可直接切回 CPU 模式。
- 修复在 LOG 回看面板播放历史台词或录音语音期间，若后台对话生成结束或刷新记录，导致正在播放的语音按钮提前丢失播放高亮且后续无法正常恢复的问题；现在回看面板在记录刷新期间会持续保留正在播放的语音状态，播放结束或停止后正确恢复。
- 修复会话最新一轮助手回复为纯日文（中文为空）时，返回标题页会跳过该轮并回退至更早历史轮次或默认占位语、丢失用户提问并隐藏「试听这句」按钮的问题；现在标题页摘录与会话列表支持以日文台词兜底展示，正确保留最新提问与试听按钮且不重复渲染副行。
- 修复对话记录（LOG）中的用户语音录音缓存已过期或被清理时，点击「播放录音」提示无法播放后按钮仍一直停留为播放中状态的问题；现在录音缺失时按钮会立即恢复默认状态。
- 修复发送提问时若后端已保存但网络或响应中途断开，刷新或重新打开网页后同一条提问重复显示两次（一条已回复、一条提示发送失败）的问题；现在断连后已保存的提问会与回复正常对应且不再重复，真正未发送成功的提问仍保留在末尾供重新发送。
- 修复在全局设置中切换模型服务商下拉选项后，未修改其他输入框直接点击底栏「保存设置」导致新服务商未被保存与启用、且界面误报保存成功的问题；现在直接点击底栏保存可正常保存并启用新选中的服务商，切回原服务商后保存也不会发起多余请求。
- 修复离线状态或重置服务商地址时 Google Gemini 与 Anthropic Claude 默认地址缺失路径后缀导致请求失败且内置预设缺少 Moonshot，以及 xAI 与 Gemini 遇到参数或上下文超长等非鉴权错误时被误提示为密钥无效的问题；现在内置预设已补齐 Moonshot 与完整地址，错误诊断可准确区分上下文超长、余额不足与真实的密钥错误。
- 修复设置页「测试连接」失败时诊断标题判错：Gemini 的 Invalid JSON payload、xAI 的提示词超长等 400 错误被显示成「API Key 凭据鉴权未通过」，请求编号里恰好含 402 的服务器错误被显示成「账户余额或额度不足」。真实的 Key 错误、余额不足和请求受限仍照常识别。
- 磁盘已满或数据目录只读时，对话、语音合成、单句重新生成以及音色创建或删除会提示检查模型密钥或语音引擎等误导信息；现在统一明确提示磁盘空间不足或数据目录只读，单句重新生成时也会提示原音频与文字已保留。
- 修复在设置中测试提供商连接失败时，若错误信息包含的用量统计或请求编号中嵌有 401、503 等状态码数字，诊断提示会被误判为 API Key 无效或服务繁忙并给出误导建议的问题；现在状态码仅按完整数字识别，恢复准确的错误归因与排查提示。
- 修复语音引擎正在启动、校准或加载模型时，首页误报「语音 未连接」与请求超时的问题；现在会等待引擎完成初始化探测，如实显示「正在加载模型」等就绪前状态。

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

[Unreleased]: https://github.com/LevenST24/galgame2voice/compare/v3.1.2...HEAD
[3.1.2]: https://github.com/LevenST24/galgame2voice/releases/tag/v3.1.2
[3.1.1]: https://github.com/LevenST24/galgame2voice/releases/tag/v3.1.1
[3.1.0]: https://github.com/LevenST24/galgame2voice/releases/tag/v3.1.0
[3.0.0]: https://github.com/LevenST24/galgame2voice/releases/tag/v3.0.0
[2.0.0]: https://github.com/LevenST24/galgame2voice/releases/tag/v2.0.0

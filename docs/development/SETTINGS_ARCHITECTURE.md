# 全局设置控制器架构与交接文档

本文档总结 T04「按业务职责拆分全局设置控制器」收口后的前端设置架构、公共兼容接口、测试覆盖路径、验证边界及版本状态。

---

## 1. 最终模块职责划分 (`frontend/src/controllers/`)

全局设置弹窗（`#globalModal`）由单体巨型脚本解耦为面向业务领域的三个独立控制器与一个轻量协调器：

| 模块 | 核心类 / 导出 | 职责与归属边界 |
| :--- | :--- | :--- |
| `global_settings.js` | 协调器 / 兼容门面 | 统筹设置弹窗生命周期、Tab 切换、全局配置读取（`/api/config`）、页脚保存编排（预约写入顺序）、私有模型连接范围，并在重新初始化时统筹释放旧控制器与事件监听器。 |
| `provider_settings.js` | `ProviderSettingsController` | 大模型提供商清单拉取、模型下拉列表、Reasoning Effort 强度、API Key 脱敏/可见性切换、提供商连通性测试及模型保存/启用。 |
| `telemetry_settings.js` | `TelemetrySettingsController` | 5 秒周期的硬件与系统遥测轮询，独立管理 GET 请求的 `AbortController`，弹窗关闭/切后台/重新初始化时中止旧读取；独立更新 GPT-SoVITS 在线状态卡片、推理精度徽标、系统 RAM、PID/运行时间与缓存容量。 |
| `engine_settings.js` | `EngineSettingsController` | GPT-SoVITS 引擎精度下拉自动保存、一键设备精度轮转切换、热重启调度（单次 POST 结合带 AbortSignal 的 `waitForEngine` 状态轮询）与离线音频缓存二次确认清理。 |
| `engine_directory.js` | `initEngineDirectory` | GPT-SoVITS 本地安装目录浏览与持久化保存，返回包含 `dispose()` 的清理接口。 |
| `settings_ui.js` | `initSettingsTabs` 等 | 标签页键盘导航与 ARIA 属性绑定，返回注销监听器的清理函数。 |

---

## 2. 公共兼容 API

`global_settings.js` 继续作为统一入口导出所有历史命名函数，保证现有界面与其他调用方无缝兼容：

- 提供商与模型相关：`getCurrentActiveProvider()`、`haveProvidersLoaded()`、`hasProviderReadError()`、`loadProviders(options)`、`onProviderChange()`、`toggleKeyVisibility()`、`onChatModelSelectChange()`、`handleProviderTest()`、`handleActivateProvider()`。
- 窗口与协调相关：`initGlobalSettings(dom, callbacks)`、`openGlobalSettings(tabName)`、`loadGlobalConfig()`、`saveGlobalConfig()`、`savePrivateEndpointPreference()`。
- 运行状态与引擎相关：`fetchSystemTelemetry()`、`handleRestartSovits(precision)`。

---

## 3. 已验证的用户关键路径

本轮独立验收通过完整 `npm test`、设置相关浏览器模块回归，以及 `test_settings_lifecycle_decomposition.mjs` 公开回归：

1. **设置中心打开与导航**：主界面/快捷菜单呼出设置中心，各 Tab 正常切换，焦点循环受控，关闭后停止后台轮询；从设置中心跳转“聊天体验”与“角色配置”再返回首页，草稿与会话历史完整保留。
2. **模型测试与保存隔离**：切换提供商或编辑参数即刻作废在途测试；迟到响应不泄露提示，不覆盖新测试按钮的忙碌状态；页脚保存能先预约模型保存再写入全局配置，脏字段得到准确保留。
3. **引擎重启与状态恢复**：发起引擎重启后单次发送 POST，有界轮询等待就绪；队列中已释放的未发送重启自动跳过，后续配置保存仍按原顺序执行；若重启在途重新初始化设置，旧等待通过 AbortSignal 安全退出且不影响服务端已确认重启；`waitForEngine` 在 progress 回调或等待入口响应取消信号即刻抛出 `AbortError`；用户可直接发起新重启；未确认的重启提示在后续就绪刷新中如实保留。
4. **多次初始化幂等解绑、忙碌控件释放与视图隔离**：在相同 DOM 控件上重复调用 `initGlobalSettings` 时，前序绑定的刷新、精度保存、Tab 切换与目录选择器监听器均被完整注销，点击控件仅触发单次请求；控制器释放时同步释放所持有的忙碌控件（引擎重启与文本、缓存清理、目录选择与保存按钮），退役请求受 `disposed` 守卫保护，不会错误提前解锁新实例中正在进行的同类操作；已退役协调器的配置 GET 请求受实例代数与 DOM 归属保护，迟到返回不覆盖新视图。

---

## 4. 真实环境与发布验证边界

- **受管引擎与 GPU 硬件**：CI 与本地测试均使用替身桩（`scripts/dev_standins.py`）及合成数据，未在真实 NVIDIA GPU 或真实 GPT-SoVITS 实例上运行深度推理或显存压力测试（实际效果由 T13 持续跟踪）。
- **立绘角色资源**：不包含完整角色服装分层与情绪资源包，依赖本地测试替身桩。
- **Windows 便携包**：最终前端部署后，用锁定的 desktop 依赖及标准 `build_windows_portable.py` 构建私有候选包。`smoke_windows_portable.py --skip-browser` 通过受限 PATH、中文/空格/感叹号路径、首次启动、FFmpeg 转换、重复启动、更新保留数据和离线恢复等检查；随后 `smoke_windows_turn_reply.py --with-browser-ui` 在该包的实际 EXE、独立 Chrome 与合成 LLM/TTS 上通过聊天身份关联、单句语音修复、重启持久化、LOG 播放、备份导出及设置导航，数据库保持一问一答两行，任务进程和端口均已收束。此项证明冻结包的合成业务流程可运行；不替代上述真实服务或听感验收。
- **发布状态**：候选包仅供本地验收，包含 Unreleased 源码，沿用项目当前版本号，未覆盖已发布的 v3.1.2，也未签名、创建新标签或发布新 Release。后续正式发布仍须执行发布工作流。

---

## 5. 版本对照状态

- **已发布版本 (v3.1.2)**：解决了 Windows 便携版启动器导航竞态与 CDP 上下文销毁问题，确立了当前稳定基线。
- **未发布改动 (Unreleased)**：
  - 历史无分句助手消息（`lines_json IS NULL`）的单句语音恢复与 409 多端同步（T03 收口）。
  - 大模型提供商控制器抽离与草稿隔离（T04 Phase 1，PR294）。
  - 遥测与引擎控制器完整解耦、`waitForEngine` 取消支持、监听器注销与相关竞态修复（T04 Phase 2 收口；原 Telegram 设置控制器已随 Telegram 机器人下线一并移除）。

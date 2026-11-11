// Provider Settings Controller: LLM Providers, Model & Key Rendering, Connection Tests, Save/Activate
import {
  fetchProviders,
  testProviderConnection,
} from '../api.js';
import { formatProviderDiagnostic, BUILTIN_PRESETS } from '../settings.js';
import { showToast } from '../ui.js';
import { saveConfirmedProvider, activateConfirmedProvider } from './provider_recovery.js';

export const PROVIDER_FIELDS = ['gApiKey', 'gBaseUrl', 'gChatModel', 'gChatModelSelect', 'gReasoningEffort', 'gCustomName'];

function isLoopbackOrPrivateIpv4(host) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if (!m) return false;
  const octets = m.slice(1).map(Number);
  if (octets.some((n) => n > 255)) return false;
  const [a, b] = octets;
  return a === 0 || a === 10 || a === 127 || (a === 169 && b === 254)
    || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127);
}

function isLoopbackOrPrivateBaseUrl(rawUrl) {
  if (!rawUrl) return false;
  let parsed;
  try { parsed = new URL(rawUrl); } catch (_) { return false; }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false;
  const hostname = parsed.hostname.replace(/^\[|\]$/g, '').toLowerCase().replace(/\.+$/, '');
  if (!hostname) return false;
  if (hostname === 'localhost' || hostname.endsWith('.localhost') || hostname.endsWith('.local')) return true;
  if (isLoopbackOrPrivateIpv4(hostname) || hostname === '::1' || hostname === '::') return true;
  if (/^fe[89ab][0-9a-f]:/.test(hostname) || /^(?:fc|fd)[0-9a-f]{0,2}:/.test(hostname)) return true;
  if (hostname.startsWith('::ffff:')) {
    const hex = /^([0-9a-f]{1,4}):([0-9a-f]{1,4})$/.exec(hostname.slice(7));
    if (hex) {
      const hi = parseInt(hex[1], 16), lo = parseInt(hex[2], 16);
      return isLoopbackOrPrivateIpv4(`${(hi >> 8) & 0xff}.${hi & 0xff}.${(lo >> 8) & 0xff}.${lo & 0xff}`);
    }
  }
  return false;
}

export class ProviderSettingsController {
  constructor(dom = {}, coordinator = {}) {
    this.dom = dom || {};
    this.coordinator = coordinator || {};
    this.cachedProviders = [];
    this.cachedPresets = [];
    this.currentActiveProvider = null;
    this.loadedProviderId = '';
    this.providersLoaded = false;
    this.providerReadError = false;
    this.providerDirty = false;
    this.providerRevision = 0;
    this.providerLoad = 0;
    this.providerSaving = false;
    this.dirtyProviderFields = new Set();
    this.providerTestRevision = 0;
    this.providerTestController = null;
    this.providerTestInitialHtml = dom.gProviderTest?.innerHTML || '<svg class="icon" style="width:14px;height:14px;vertical-align:-2px;"><use href="#i-activity"></use></svg><span>测试连通性</span>';
    this.disposed = false;
    this.cleanups = [];

    this._bindEvents();
  }

  _bind(element, type, handler) {
    if (!element || typeof element.addEventListener !== 'function') return;
    const bound = (...args) => {
      if (this.disposed) return;
      return handler(...args);
    };
    element.addEventListener(type, bound);
    this.cleanups.push(() => {
      if (typeof element.removeEventListener === 'function') {
        element.removeEventListener(type, bound);
      }
    });
  }

  _bindEvents() {
    if (this.dom.gProvider) this._bind(this.dom.gProvider, 'change', () => this.onProviderChange());
    if (this.dom.gBtnToggleKeyVisibility) this._bind(this.dom.gBtnToggleKeyVisibility, 'click', () => this.toggleKeyVisibility());
    if (this.dom.gChatModelSelect) this._bind(this.dom.gChatModelSelect, 'change', () => this.onChatModelSelectChange());
    if (this.dom.gProviderTest) this._bind(this.dom.gProviderTest, 'click', () => this.handleProviderTest());
    if (this.dom.gProviderActivate) this._bind(this.dom.gProviderActivate, 'click', () => this.handleActivateProvider());
    if (this.dom.gAllowPrivateEndpoints) {
      this._bind(this.dom.gAllowPrivateEndpoints, 'change', () => {
        if (this.dom.gAllowPrivateEndpoints.checked) this.resetTestResultBox();
      });
    }
    for (const field of PROVIDER_FIELDS) {
      for (const event of ['input', 'change']) {
        if (this.dom[field]) {
          this._bind(this.dom[field], event, () => this.markProviderDirty(field));
        }
      }
    }
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.invalidateActiveProviderTest();
    this.providerLoad += 1;
    this.providerRevision += 1;
    if (this.dom.gBtnResetBaseUrl) {
      this.dom.gBtnResetBaseUrl.onclick = null;
    }
    for (const cleanup of this.cleanups) {
      try { cleanup(); } catch (_) {}
    }
    this.cleanups = [];
    this.dom = {};
  }

  getCurrentActiveProvider() {
    return this.currentActiveProvider;
  }

  haveProvidersLoaded() {
    return this.providersLoaded;
  }

  hasProviderReadError() {
    return this.providerReadError;
  }

  isProviderDirty() {
    return this.providerDirty;
  }

  isProviderSaving() {
    return this.providerSaving;
  }

  getProviderRevision() {
    return this.providerRevision;
  }

  invalidateActiveProviderTest() {
    this.providerTestRevision += 1;
    if (this.providerTestController) {
      this.providerTestController.abort();
      this.providerTestController = null;
    }
    if (this.dom.gProviderTest) {
      this.dom.gProviderTest.disabled = false;
      if (this.providerTestInitialHtml) this.dom.gProviderTest.innerHTML = this.providerTestInitialHtml;
    }
  }

  resetTestResultBox() {
    if (this.dom.gProviderTestResult) {
      this.dom.gProviderTestResult.className = 'test-result-box hidden';
      if (this.dom.gTestResultBody) this.dom.gTestResultBody.textContent = '';
    }
  }

  _promptEnablePrivateEndpoints() {
    if (this.dom.gProviderTestResult) this.dom.gProviderTestResult.className = 'test-result-box test-result-error';
    if (this.dom.gTestResultTitle) this.dom.gTestResultTitle.textContent = '尚未开启本机 / 局域网连接权限';
    if (this.dom.gTestResultLatency) this.dom.gTestResultLatency.textContent = '未发送';
    if (this.dom.gTestResultBody) {
      this.dom.gTestResultBody.textContent =
        '当前填写的是本机或局域网地址。请先勾选本页「允许连接本机或局域网的模型服务」开关，再测试连通性或保存。';
    }
    if (this.dom.gPrivateEndpointsStatus) {
      this.dom.gPrivateEndpointsStatus.textContent = '请先勾选此项以连接本机或局域网模型服务';
    }
    this.dom.gAllowPrivateEndpoints.scrollIntoView({ block: 'nearest' });
    this.dom.gAllowPrivateEndpoints.focus();
  }

  isProviderTestCurrent(revision, snapshot) {
    if (this.disposed) return false;
    if (revision !== this.providerTestRevision) return false;
    if (this.coordinator.settingsVisible && !this.coordinator.settingsVisible()) return false;
    if (!this.dom.gProvider || this.dom.gProvider.value !== snapshot.rawPid) return false;
    if ((this.dom.gBaseUrl?.value.trim() || '') !== snapshot.baseUrl) return false;
    if ((this.dom.gChatModel?.value.trim() || '') !== snapshot.chatModel) return false;
    if ((this.dom.gApiKey?.value.trim() || '') !== snapshot.apiKey) return false;
    if ((this.dom.gReasoningEffort?.value || '') !== snapshot.reasoningEffort) return false;
    return true;
  }

  markProviderDirty(field) {
    if (this.disposed) return;
    this.providerDirty = true;
    this.providerRevision += 1;
    this.dirtyProviderFields.add(field);
    if (field === 'gChatModelSelect') this.dirtyProviderFields.add('gChatModel');
    if (PROVIDER_FIELDS.includes(field)) {
      this.invalidateActiveProviderTest();
      this.resetTestResultBox();
    }
  }

  onProviderChange() {
    if (this.disposed) return;
    this.invalidateActiveProviderTest();
    this.providerRevision += 1;
    this.dirtyProviderFields.clear();
    const pid = this.dom.gProvider?.value;
    const realId = (pid === '__custom__' || pid === 'custom') ? 'custom' : (pid || '');
    const baselineId = this.currentActiveProvider?.id || this.loadedProviderId || '';
    this.providerDirty = Boolean(realId && baselineId && realId !== baselineId);
    this.renderProviderForm();
  }

  renderLoadedProvider(keepDraft) {
    if (this.disposed) return;
    this.renderProviderForm(keepDraft);
    if (!keepDraft) {
      const rawPid = this.dom.gProvider?.value;
      const normPid = (rawPid === '__custom__' || rawPid === 'custom') ? 'custom' : (rawPid || '');
      this.loadedProviderId = normPid || this.currentActiveProvider?.id || '';
      this.providerDirty = false;
      this.dirtyProviderFields.clear();
    }
  }

  renderProviderForm(keepDraft = false) {
    if (this.disposed || !this.dom.gProvider) return;
    const fill = (field, apply) => {
      if (this.dom[field] && !(keepDraft && this.dirtyProviderFields.has(field))) apply(this.dom[field]);
    };
    const pid = this.dom.gProvider.value;
    const isCustom = pid === 'custom' || pid === '__custom__';
    const realId = isCustom ? 'custom' : pid;

    const stored = this.cachedProviders.find((p) => p.id === realId);
    const preset = this.cachedPresets.find((p) => p.id === realId) || BUILTIN_PRESETS.find((p) => p.id === realId);

    // 1. 自定义提供商名称字段显隐
    if (this.dom.gCustomNameField) {
      this.dom.gCustomNameField.classList.toggle('hidden', !isCustom);
      fill('gCustomName', el => { el.value = isCustom ? stored?.name || '自定义模型' : ''; });
    }

    // 2. 当前生效状态徽标与描述文本
    const isActive = Boolean(stored?.is_active);
    if (this.dom.gProviderActiveBadge) {
      this.dom.gProviderActiveBadge.className = `badge-status-pill ${isActive ? 'badge-pill-green' : 'badge-pill-gray'}`;
      this.dom.gProviderActiveBadge.textContent = isActive ? '当前生效' : '未启用';
    }
    if (this.dom.gProviderActive) {
      this.dom.gProviderActive.textContent = isActive ? `当前：${stored ? stored.name : realId} · ${stored?.chat_model || ''}` : '';
    }
    if (this.dom.gProviderDesc) {
      this.dom.gProviderDesc.textContent =
        preset?.description ||
        stored?.description ||
        (isCustom ? '本地或私有部署的 OpenAI 兼容推理服务 (Ollama / vLLM / LMStudio)' : '外部 AI 大模型服务提供商');
    }

    // 3. API Key 状态与脱敏占位符处理
    const keepKey = keepDraft && this.dirtyProviderFields.has('gApiKey');
    fill('gApiKey', el => { el.value = ''; el.type = 'password'; });
    if (this.dom.gBtnToggleKeyText && !keepKey) {
      this.dom.gBtnToggleKeyText.textContent = '显示';
    }
    if (this.dom.gIconKeyEye && !keepKey) {
      const use = this.dom.gIconKeyEye.querySelector('use');
      if (use) use.setAttribute('href', '#i-eye');
    }

    const maskedKey = (stored?.api_key || '').trim();
    const hasKey = Boolean(maskedKey && maskedKey.length > 0);

    if (hasKey) {
      if (this.dom.gApiKey) this.dom.gApiKey.placeholder = `已配置：${maskedKey}（留空保持原密钥，输入新密钥可覆盖）`;
      if (this.dom.gProviderKeyTag) this.dom.gProviderKeyTag.className = 'provider-status-tag tag-configured';
      if (this.dom.gKeyStatusText) this.dom.gKeyStatusText.textContent = `已配置密钥 (${maskedKey})`;
      if (this.dom.gApiKeyTip) this.dom.gApiKeyTip.textContent = '密钥已保存。如需修改，请在此输入新密钥后保存。';
    } else if (isCustom) {
      if (this.dom.gApiKey) this.dom.gApiKey.placeholder = 'sk-…（本地 Ollama / vLLM 等无鉴权服务可留空）';
      if (this.dom.gProviderKeyTag) this.dom.gProviderKeyTag.className = 'provider-status-tag tag-optional';
      if (this.dom.gKeyStatusText) this.dom.gKeyStatusText.textContent = '可选 (本地服务可免密)';
      if (this.dom.gApiKeyTip) this.dom.gApiKeyTip.textContent = '本地服务（如 Ollama）无需填写，公网中转或鉴权服务请输入对应凭据。';
    } else {
      if (this.dom.gApiKey) this.dom.gApiKey.placeholder = `请输入 ${preset?.name || pid} 的有效 API Key (必填)`;
      if (this.dom.gProviderKeyTag) this.dom.gProviderKeyTag.className = 'provider-status-tag tag-unconfigured';
      if (this.dom.gKeyStatusText) this.dom.gKeyStatusText.textContent = '未配置 API Key · 无法调用';
      if (this.dom.gApiKeyTip) this.dom.gApiKeyTip.textContent = '该提供商尚未配置密钥。请前往官方控制台申领并在此填入。';
    }

    // 4. Base URL 填充与一键恢复默认
    const defaultUrl = preset?.default_base_url || (isCustom ? 'http://127.0.0.1:11434/v1' : 'https://api.openai.com/v1');
    const currentUrl = stored?.api_base_url || defaultUrl;
    fill('gBaseUrl', el => { el.value = currentUrl; el.placeholder = defaultUrl; });
    if (this.dom.gBtnResetBaseUrl) {
      this.dom.gBtnResetBaseUrl.textContent = '重置为默认';
      this.dom.gBtnResetBaseUrl.title = `恢复官方默认端点: ${defaultUrl}`;
      this.dom.gBtnResetBaseUrl.onclick = () => {
        if (this.disposed) return;
        if (this.dom.gBaseUrl) {
          this.dom.gBaseUrl.value = defaultUrl;
          this.markProviderDirty('gBaseUrl');
        }
        showToast(`已恢复官方默认 Base URL: ${defaultUrl}`, 'info');
      };
    }

    // 5. 对话模型选择
    const presetModels = preset?.preset_models || (stored?.chat_model ? [stored.chat_model] : []);
    const keepModelSelect = keepDraft && this.dirtyProviderFields.has('gChatModelSelect');
    if (this.dom.gChatModelSelect && !keepModelSelect) {
      this.dom.gChatModelSelect.innerHTML = '';
      const placeholderOpt = document.createElement('option');
      placeholderOpt.value = '';
      placeholderOpt.textContent = '-- 选择预设推荐模型 --';
      this.dom.gChatModelSelect.appendChild(placeholderOpt);
      presetModels.forEach((m) => {
        const opt = document.createElement('option');
        opt.value = m;
        opt.textContent = m;
        this.dom.gChatModelSelect.appendChild(opt);
      });
      const customModelOpt = document.createElement('option');
      customModelOpt.value = '__custom_model__';
      customModelOpt.textContent = '✏️ 手动输入任意模型 ID...';
      this.dom.gChatModelSelect.appendChild(customModelOpt);
    }

    const currentModel = keepDraft && this.dirtyProviderFields.has('gChatModel') ? this.dom.gChatModel.value
      : stored?.chat_model || preset?.default_chat_model || presetModels[0] || '';
    fill('gChatModel', el => { el.value = currentModel; });
    if (this.dom.gChatModelSelect && !keepModelSelect) {
      if (presetModels.includes(currentModel)) {
        this.dom.gChatModelSelect.value = currentModel;
      } else if (currentModel) {
        this.dom.gChatModelSelect.value = '__custom_model__';
      } else {
        this.dom.gChatModelSelect.value = '';
      }
    }

    // 6. 思考强度设置
    fill('gReasoningEffort', el => { el.value = stored ? (stored.reasoning_effort || '') : (preset?.default_reasoning_effort || ''); });

    // 7. 诊断输出框重置
    this.resetTestResultBox();
  }

  toggleKeyVisibility() {
    if (this.disposed || !this.dom.gApiKey) return;
    const isPass = this.dom.gApiKey.type === 'password';
    this.dom.gApiKey.type = isPass ? 'text' : 'password';
    if (this.dom.gBtnToggleKeyText) {
      this.dom.gBtnToggleKeyText.textContent = isPass ? '隐藏' : '显示';
    }
    if (this.dom.gIconKeyEye) {
      const use = this.dom.gIconKeyEye.querySelector('use');
      if (use) use.setAttribute('href', isPass ? '#i-eye-off' : '#i-eye');
    }
  }

  onChatModelSelectChange() {
    if (this.disposed || !this.dom.gChatModelSelect || !this.dom.gChatModel) return;
    const val = this.dom.gChatModelSelect.value;
    if (val && val !== '__custom_model__') {
      this.dom.gChatModel.value = val;
    } else if (val === '__custom_model__') {
      this.dom.gChatModel.focus();
      this.dom.gChatModel.select();
    }
  }

  async loadProviders({ formRevision = this.providerRevision } = {}) {
    if (this.disposed) return;
    const load = ++this.providerLoad;
    const hadOptions = Boolean(this.dom.gProvider?.children?.length);
    if (this.dom.gProvider && !hadOptions) this.dom.gProvider.innerHTML = '<option value="">加载模型服务列表…</option>';
    try {
      const data = await fetchProviders();
      if (load !== this.providerLoad || this.disposed) return;
      this.cachedProviders = data && data.providers ? data.providers : [];
      this.cachedPresets = data && data.presets && data.presets.length ? data.presets : BUILTIN_PRESETS;

      const active = this.cachedProviders.find((p) => p.is_active);
      this.currentActiveProvider = active || null;
      this.providersLoaded = true;
      this.providerReadError = false;
      if (this.coordinator.updateBadge) this.coordinator.updateBadge();

      const presetOrder = ['gemini', 'openai', 'anthropic', 'deepseek', 'xai', 'siliconflow', 'glm', 'qwen', 'moonshot', 'custom'];
      const providerMap = new Map();

      presetOrder.forEach((pid) => {
        const preset = this.cachedPresets.find((p) => p.id === pid);
        const stored = this.cachedProviders.find((p) => p.id === pid);
        if (preset || stored) {
          providerMap.set(pid, {
            id: pid,
            name: stored?.name || preset?.name || pid,
            chat_model: stored?.chat_model || preset?.default_chat_model || '',
            is_active: Boolean(stored?.is_active),
          });
        }
      });

      this.cachedPresets.forEach((p) => {
        if (!providerMap.has(p.id) && p.id !== 'custom') {
          const stored = this.cachedProviders.find((sp) => sp.id === p.id);
          providerMap.set(p.id, {
            id: p.id,
            name: stored?.name || p.name || p.id,
            chat_model: stored?.chat_model || p.default_chat_model || '',
            is_active: Boolean(stored?.is_active),
          });
        }
      });

      this.cachedProviders.forEach((p) => {
        if (!providerMap.has(p.id)) {
          providerMap.set(p.id, {
            id: p.id,
            name: p.name,
            chat_model: p.chat_model || '',
            is_active: Boolean(p.is_active),
          });
        }
      });

      const items = Array.from(providerMap.values());
      const sorted = active
        ? [items.find((i) => i.id === active.id) || active, ...items.filter((i) => i.id !== active.id)]
        : items;

      const keepDraft = this.providerDirty || this.providerRevision !== formRevision;
      const selection = keepDraft ? this.dom.gProvider?.value : null;
      if (this.dom.gProvider) {
        this.dom.gProvider.innerHTML = '';
        sorted.forEach((p) => {
          const opt = document.createElement('option');
          opt.value = p.id;
          opt.textContent = `${p.name} · ${p.chat_model || '未设定'}${p.is_active ? '（当前生效）' : ''}`;
          if (p.is_active) opt.selected = true;
          this.dom.gProvider.appendChild(opt);
        });
        const customOpt = document.createElement('option');
        customOpt.value = '__custom__';
        customOpt.textContent = '＋ 自定义模型 / 接口…';
        if (!active && !sorted.length) customOpt.selected = true;
        this.dom.gProvider.appendChild(customOpt);
        if (selection && [...this.dom.gProvider.children].some(option => option.value === selection)) {
          this.dom.gProvider.value = selection;
        }
      }

      this.renderLoadedProvider(keepDraft);
    } catch (e) {
      if (load !== this.providerLoad || this.disposed) return;
      console.error('Failed to load providers:', e);
      this.providersLoaded = true;
      this.providerReadError = true;
      if (this.coordinator.updateBadge) this.coordinator.updateBadge();
      if (hadOptions) {
        showToast(`列表暂时无法刷新，当前输入已保留：${e.message}`, 'error');
        return;
      }
      if (this.dom.gProvider) {
        this.dom.gProvider.innerHTML = '';
        BUILTIN_PRESETS.forEach((p) => {
          const opt = document.createElement('option');
          opt.value = p.id;
          opt.textContent = `${p.name} · ${p.default_chat_model}`;
          this.dom.gProvider.appendChild(opt);
        });
        const customOpt = document.createElement('option');
        customOpt.value = '__custom__';
        customOpt.textContent = '＋ 自定义模型 / 接口…';
        this.dom.gProvider.appendChild(customOpt);
      }
      showToast('模型服务列表加载失败：请检查网络后重试。', 'error');
      this.renderLoadedProvider(this.providerDirty || this.providerRevision !== formRevision);
    }
  }

  async handleProviderTest() {
    if (this.disposed || !this.dom.gProviderTest || !this.dom.gProvider) return;
    if (this.coordinator.isConnectionPreferencePending && this.coordinator.isConnectionPreferencePending()) return;

    this.invalidateActiveProviderTest();
    const rawPid = this.dom.gProvider.value;
    const pid = rawPid === '__custom__' ? 'custom' : rawPid;
    const baseUrl = this.dom.gBaseUrl ? this.dom.gBaseUrl.value.trim() : '';
    if (
      pid === 'custom' &&
      this.dom.gAllowPrivateEndpoints &&
      !this.dom.gAllowPrivateEndpoints.checked &&
      isLoopbackOrPrivateBaseUrl(baseUrl)
    ) {
      this._promptEnablePrivateEndpoints();
      showToast('请先勾选「允许连接本机或局域网的模型服务」后再测试连接', 'warning');
      return;
    }

    const revision = ++this.providerTestRevision;
    const controller = new AbortController();
    this.providerTestController = controller;

    const chatModel = this.dom.gChatModel ? this.dom.gChatModel.value.trim() : '';
    const apiKey = this.dom.gApiKey ? this.dom.gApiKey.value.trim() : '';
    const reasoningEffort = this.dom.gReasoningEffort ? this.dom.gReasoningEffort.value : '';

    const snapshot = {
      rawPid,
      pid,
      baseUrl,
      chatModel,
      apiKey,
      reasoningEffort,
    };

    this.dom.gProviderTest.disabled = true;
    if (!this.providerTestInitialHtml && this.dom.gProviderTest.innerHTML && !this.dom.gProviderTest.innerHTML.includes('测试中')) {
      this.providerTestInitialHtml = this.dom.gProviderTest.innerHTML;
    }
    this.dom.gProviderTest.innerHTML =
      '<svg class="icon" style="width:14px;height:14px;vertical-align:-2px;"><use href="#i-activity"></use></svg><span>测试中…</span>';

    const payload = {
      id: pid,
      api_base_url: baseUrl,
      chat_model: chatModel,
      reasoning_effort: reasoningEffort || null,
    };
    if (apiKey && !apiKey.includes('****')) {
      payload.api_key = apiKey;
    }

    try {
      const data = await testProviderConnection(payload, { signal: controller.signal });

      if (!this.isProviderTestCurrent(revision, snapshot)) return;

      if (this.dom.gProviderTestResult) this.dom.gProviderTestResult.classList.remove('hidden');

      if (data.success) {
        const lat = Math.round(data.latency_ms || 0);
        showToast(`连通性测试成功（耗时 ${lat} ms）`, 'success');
        if (this.dom.gProviderTestResult) {
          this.dom.gProviderTestResult.className = 'test-result-box test-result-success';
        }
        if (this.dom.gTestResultTitle) this.dom.gTestResultTitle.textContent = '连通性测试通过';
        if (this.dom.gTestResultLatency) this.dom.gTestResultLatency.textContent = `耗时 ${lat}ms`;
        if (this.dom.gTestResultBody) {
          const modelsCount = Array.isArray(data.models) ? data.models.length : 0;
          this.dom.gTestResultBody.textContent =
            `已成功连接至目标端点，模型 ${chatModel || '默认'} 就绪。` +
            (modelsCount > 0 ? ` (发现 ${modelsCount} 个可用模型)` : '');
        }
      } else {
        const rawErr = data.error || data.message || '连接失败';
        const diag = formatProviderDiagnostic(pid, rawErr, data.diagnostic);
        showToast(`连通性测试未通过: ${diag.title}`, 'error');
        if (this.dom.gProviderTestResult) {
          this.dom.gProviderTestResult.className = 'test-result-box test-result-error';
        }
        if (this.dom.gTestResultTitle) this.dom.gTestResultTitle.textContent = diag.title;
        if (this.dom.gTestResultLatency) this.dom.gTestResultLatency.textContent = '失败';
        if (this.dom.gTestResultBody) this.dom.gTestResultBody.textContent = diag.guidance;
      }
    } catch (e) {
      if (e.name === 'AbortError' || controller.signal.aborted) {
        return;
      }
      if (!this.isProviderTestCurrent(revision, snapshot)) return;
      showToast(`测试请求异常: ${e.message || e}`, 'error');
      if (this.dom.gProviderTestResult) {
        this.dom.gProviderTestResult.className = 'test-result-box test-result-error';
        this.dom.gProviderTestResult.classList.remove('hidden');
        if (this.dom.gTestResultTitle) this.dom.gTestResultTitle.textContent = '网络请求异常';
        if (this.dom.gTestResultLatency) this.dom.gTestResultLatency.textContent = '异常';
        if (this.dom.gTestResultBody) this.dom.gTestResultBody.textContent = `请求发送失败: ${e.message || e}`;
      }
    } finally {
      if (revision === this.providerTestRevision && !this.disposed) {
        if (this.providerTestController === controller) this.providerTestController = null;
        if (this.dom.gProviderTest) {
          this.dom.gProviderTest.disabled = false;
          if (this.providerTestInitialHtml) this.dom.gProviderTest.innerHTML = this.providerTestInitialHtml;
        }
      }
    }
  }

  async handleActivateProvider() {
    return this.saveAndActivateProvider();
  }

  // The footer save reuses this; it reports success itself and must say the rest was not saved either.
  async saveAndActivateProvider({ fromFooter = false } = {}) {
    if (this.disposed || !this.dom.gProviderActivate || !this.dom.gProvider) return false;
    const configSaving = this.coordinator.isConfigSaving ? this.coordinator.isConfigSaving() : false;
    if (this.providerSaving || (configSaving && !fromFooter)) return false;
    if (this.coordinator.isConnectionPreferencePending && this.coordinator.isConnectionPreferencePending()) return false;
    const savedRevision = this.providerRevision;
    this.providerSaving = true;
    this.coordinator.updateSaveButtons?.();
    const origHtml = this.dom.gProviderActivate.innerHTML;
    this.dom.gProviderActivate.innerHTML =
      '<svg class="icon" style="width:14px;height:14px;vertical-align:-2px;"><use href="#i-check"></use></svg><span>保存并启用中…</span>';

    try {
      const selected = this.dom.gProvider.value;
      const isCustom = selected === '__custom__' || selected === 'custom';
      const pid = isCustom ? 'custom' : selected;

      const baseUrl = this.dom.gBaseUrl ? this.dom.gBaseUrl.value.trim() : '';
      const chatModel = this.dom.gChatModel ? this.dom.gChatModel.value.trim() : '';
      const apiKey = this.dom.gApiKey ? this.dom.gApiKey.value.trim() : '';
      const reasoningEffort = this.dom.gReasoningEffort ? this.dom.gReasoningEffort.value : null;
      const customName = this.dom.gCustomName ? this.dom.gCustomName.value.trim() : '';

      if (!baseUrl) {
        throw new Error('请填写 API Base URL');
      }
      if (!chatModel) {
        throw new Error('请填写或选择模型名称');
      }
      if (
        isCustom &&
        this.dom.gAllowPrivateEndpoints &&
        !this.dom.gAllowPrivateEndpoints.checked &&
        isLoopbackOrPrivateBaseUrl(baseUrl)
      ) {
        this._promptEnablePrivateEndpoints();
        showToast(`请先勾选「允许连接本机或局域网的模型服务」后再保存启用${fromFooter ? '；其他设置也还没有保存' : ''}`, 'warning');
        return false;
      }

      const stored = this.cachedProviders.find((p) => p.id === pid);
      const preset = this.cachedPresets.find((p) => p.id === pid) || BUILTIN_PRESETS.find((p) => p.id === pid);

      const hasExistingKey = Boolean(stored?.api_key && stored.api_key.length > 0);
      if (!isCustom && !hasExistingKey && !apiKey) {
        if (this.dom.gApiKey) this.dom.gApiKey.focus();
        throw new Error(`请先输入 ${preset?.name || pid} 的 API Key 再启用`);
      }

      const payload = {
        id: pid,
        name: isCustom ? customName || '自定义模型' : preset?.name || stored?.name || pid,
        api_base_url: baseUrl,
        chat_model: chatModel,
        reasoning_effort: reasoningEffort || null,
        is_active: true,
      };
      if (apiKey && !apiKey.includes('****')) {
        payload.api_key = apiKey;
      }

      const saved = await saveConfirmedProvider(payload);
      if (this.disposed) return true;

      // is_active is committed with the fields by POST /providers. A second
      // activation can fail after success, or overwrite a later tab's selection.
      let active;
      if (saved?.provider?.id === pid && saved.provider.is_active === true) {
        active = saved.provider;
      } else {
        if (this.disposed) return true;
        const activated = await activateConfirmedProvider(payload);
        if (this.disposed) return true;
        active = activated.active_provider;
      }

      if (this.disposed) return true;

      this.currentActiveProvider = active || { name: payload.name, chat_model: chatModel };
      this.loadedProviderId = active?.id || pid;
      if (this.coordinator.updateBadge) this.coordinator.updateBadge();

      if (savedRevision === this.providerRevision && !this.disposed) {
        this.providerDirty = false;
        this.dirtyProviderFields.clear();
      }
      await this.loadProviders({ formRevision: savedRevision });
      if (this.disposed) return true;

      if (!fromFooter) showToast(`已成功保存并启用：${payload.name} · ${chatModel}${this.providerDirty ? '；当前修改尚未保存。' : ''}`, 'success');
      return true;
    } catch (e) {
      if (this.disposed) return false;
      console.error('Failed to activate provider:', e);
      const message = e.code === 'PROVIDER_UNCONFIRMED' ? e.message : `模型设置未保存或启用失败：${e.message || e}`;
      showToast(`${message}${fromFooter ? '；其他设置也还没有保存' : ''}`, 'error');
      return false;
    } finally {
      this.providerSaving = false;
      if (!this.disposed) {
        this.coordinator.updateSaveButtons?.();
        if (this.dom.gProviderActivate) {
          this.dom.gProviderActivate.innerHTML = origHtml;
        }
      }
    }
  }
}

export function createProviderSettingsController(dom, coordinator) {
  return new ProviderSettingsController(dom, coordinator);
}

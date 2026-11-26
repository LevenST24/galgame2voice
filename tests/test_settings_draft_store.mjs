import assert from 'node:assert/strict';
import test from 'node:test';

test('providerTestResultFromError keeps structured diagnostics from a rejected response', async () => {
  const { providerTestResultFromError } = await import('../frontend/src/api.js');
  const result = providerTestResultFromError({
    status: 400,
    message: '被拒绝',
    data: {
      error: '被拒绝',
      error_code: 'PRIVATE_ENDPOINT_BLOCKED',
      diagnostic_code: 'PRIVATE_ENDPOINT_BLOCKED',
      summary_zh: '不能访问该地址',
      action_zh: '请改用公网地址',
      retryable: false,
    },
  });
  assert.equal(result.success, false);
  assert.equal(result.diagnostic_code, 'PRIVATE_ENDPOINT_BLOCKED');
  assert.equal(result.summary_zh, '不能访问该地址');
  assert.equal(result.action_zh, '请改用公网地址');
  assert.equal(result.retryable, false);
});

test('commitAll saves the engine directory, then global config, without saving dirty model drafts', async () => {
  const { createSettingsDraftStore } = await import('../frontend/src/controllers/settings_draft_store.js');
  const order = [];
  const toasts = [];
  let configSaving = false;
  let dirDirty = true;
  let dirResult = true;
  const directory = {
    isDirty: () => dirDirty,
    async save() {
      order.push('directory.save');
      if (dirResult) dirDirty = false;
      return dirResult;
    },
  };
  const provider = {
    disposed: false,
    isProviderSaving: () => false,
    async saveAndActivateProvider() {
      order.push('saveAndActivateProvider');
      return true;
    },
  };
  const store = createSettingsDraftStore({
    isConfigSaving: () => configSaving,
    getProviderController: () => provider,
    isConfigLoaded: () => true,
    showToast: (msg, type) => { toasts.push({ msg, type }); },
    reloadConfig: () => {},
    getEngineDirectory: () => directory,
    getCoordinatorRevision: () => 1,
    setConfigSaving: (saving) => { configSaving = saving; },
    getConfigRevision: () => 1,
    readConfigPayload: () => ({ audio_retention_minutes: 30 }),
    runSettingsMutation: async (operation) => operation(async () => {
      order.push('config');
    }),
    clearConfigDirtyIfUnchanged: () => {},
    isConfigDirty: () => false,
    refreshTelemetry: () => {},
  });

  await store.commitAll();
  assert.deepEqual(order, ['directory.save', 'config']);
  assert.deepEqual(toasts, [{ msg: '全局配置已保存', type: 'success' }]);

  order.length = 0;
  toasts.length = 0;
  dirDirty = true;
  dirResult = false;
  await store.commitAll();
  assert.deepEqual(order, ['directory.save']);
});

test('a refused audio retention value saves nothing, not even a dirty engine directory', async () => {
  const { createSettingsDraftStore } = await import('../frontend/src/controllers/settings_draft_store.js');
  const { readRetentionMinutes } = await import('../frontend/src/controllers/global_settings.js');
  for (const raw of ['-5', '0', '', '1.5', '10081']) {
    assert.throws(() => readRetentionMinutes(raw), /1 到 10080 之间的整数分钟/, raw);
  }
  assert.equal(readRetentionMinutes('60'), 60);
  const order = [];
  const toasts = [];
  const store = createSettingsDraftStore({
    isConfigSaving: () => false,
    getProviderController: () => null,
    isConfigLoaded: () => true,
    showToast: (msg, type) => { toasts.push({ msg, type }); },
    reloadConfig: () => {},
    getEngineDirectory: () => ({ isDirty: () => true, async save() { order.push('directory.save'); return true; } }),
    getCoordinatorRevision: () => 1,
    setConfigSaving: () => {},
    getConfigRevision: () => 1,
    readConfigPayload: () => ({ audio_retention_minutes: readRetentionMinutes('-5') }),
    runSettingsMutation: async (operation) => operation(async () => { order.push('config'); }),
    clearConfigDirtyIfUnchanged: () => {},
    isConfigDirty: () => false,
    refreshTelemetry: () => {},
  });
  // The autosave loop stops on 'failed', so a refused value is not retried behind the user's back.
  assert.equal(await store.commitAll(), 'failed');
  assert.equal(await store.commitAll({ autosave: true }), 'failed');
  assert.deepEqual(order, []);
  assert.equal(toasts.length, 2);
  assert.ok(toasts.every((toast) => toast.type === 'error'));
  assert.match(toasts[0].msg, /音频缓存保留时间/);
});

for (const [source, where] of [['dotenv', /\.env/], ['env', /环境变量/]]) {
  test(`saving a 语音服务地址 that ${source} overrides says it did not take effect`, async () => {
    const { createSettingsDraftStore } = await import('../frontend/src/controllers/settings_draft_store.js');
    const toasts = [];
    const store = createSettingsDraftStore({
      isConfigSaving: () => false,
      getProviderController: () => null,
      isConfigLoaded: () => true,
      showToast: (message, type) => toasts.push({ message, type }),
      reloadConfig: () => {},
      getEngineDirectory: () => null,
      getCoordinatorRevision: () => 1,
      setConfigSaving: () => {},
      getConfigRevision: () => 1,
      readConfigPayload: () => ({ gpt_sovits_url: 'http://127.0.0.1:9880' }),
      // What POST /api/config answers when .env or an environment variable keeps another address.
      runSettingsMutation: (operation) => operation(async () => ({
        status: 'success', settings: { gpt_sovits_url: 'http://127.0.0.1:9880' },
        effective_sovits_url: 'http://192.168.1.50:9880', effective_sovits_source: source,
      })),
      clearConfigDirtyIfUnchanged: () => {},
      isConfigDirty: () => false,
      refreshTelemetry: () => {},
    });
    await store.commitAll();
    assert.equal(toasts.length, 1);
    assert.notEqual(toasts[0].type, 'success');
    assert.match(toasts[0].message, /未生效/);
    assert.match(toasts[0].message, /192\.168\.1\.50:9880/);
    assert.match(toasts[0].message, where);
    assert.match(toasts[0].message, /\bGPT_SOVITS_BASE_URL\b/);
  });
}

test('saving a 语音服务地址 that takes effect is a plain success', async () => {
  const { sovitsUrlOverrideNotice } = await import('../frontend/src/controllers/settings_draft_store.js');
  assert.equal(sovitsUrlOverrideNotice(
    { gpt_sovits_url: 'http://127.0.0.1:9880/' },
    { effective_sovits_url: 'http://127.0.0.1:9880', effective_sovits_source: 'db' },
  ), '');
  assert.equal(sovitsUrlOverrideNotice({ audio_retention_minutes: 30 }, { effective_sovits_url: 'http://x:1' }), '');
});

test('saving the same 语音服务地址 with omitted default port or different case is not reported as overridden', async () => {
  const { sovitsUrlOverrideNotice } = await import('../frontend/src/controllers/settings_draft_store.js');
  assert.equal(sovitsUrlOverrideNotice(
    { gpt_sovits_url: 'https://sovits.example.com' },
    { effective_sovits_url: 'https://sovits.example.com:443', effective_sovits_source: 'env' },
  ), '');
  assert.equal(sovitsUrlOverrideNotice(
    { gpt_sovits_url: 'http://192.168.1.50' },
    { effective_sovits_url: 'http://192.168.1.50:9880', effective_sovits_source: 'env' },
  ), '');
  assert.equal(sovitsUrlOverrideNotice(
    { gpt_sovits_url: 'HTTP://LOCALHOST:9880/' },
    { effective_sovits_url: 'http://localhost:9880', effective_sovits_source: 'env' },
  ), '');
  assert.match(sovitsUrlOverrideNotice(
    { gpt_sovits_url: 'http://192.168.1.50:80' },
    { effective_sovits_url: 'http://192.168.1.50:9880', effective_sovits_source: 'env' },
  ), /未生效/);
});

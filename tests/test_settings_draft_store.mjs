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
  await store.commitAll();
  assert.deepEqual(order, []);
  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].type, 'error');
  assert.match(toasts[0].msg, /音频缓存保留时间/);
});

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

test('commitAll saves the engine directory, then the model, then global config', async () => {
  const { createSettingsDraftStore } = await import('../frontend/src/controllers/settings_draft_store.js');
  const order = [];
  let modelSaved = true;
  let configSaving = false;
  const directory = {
    isDirty: () => true,
    async save() {
      order.push('directory.save');
      return true;
    },
  };
  const provider = {
    disposed: false,
    isProviderSaving: () => false,
    isProviderDirty: () => true,
    async saveAndActivateProvider(options) {
      order.push('saveAndActivateProvider');
      order.push(options);
      return modelSaved;
    },
  };
  const store = createSettingsDraftStore({
    isConfigSaving: () => configSaving,
    getProviderController: () => provider,
    isConfigLoaded: () => true,
    showToast: () => {},
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
  assert.deepEqual(order, [
    'directory.save',
    'saveAndActivateProvider',
    { fromFooter: true },
    'config',
  ]);

  order.length = 0;
  modelSaved = false;
  await store.commitAll();
  assert.deepEqual(order, [
    'directory.save',
    'saveAndActivateProvider',
    { fromFooter: true },
  ]);
});

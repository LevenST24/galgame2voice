import assert from 'node:assert/strict';
import fs from 'node:fs';
import { test } from 'node:test';

import { fakeDocument } from './helpers/adv_dom.mjs';

const contractPath = new URL('./contracts/provider_presets_v1.json', import.meta.url);
const contract = JSON.parse(fs.readFileSync(contractPath, 'utf8'));

const { BUILTIN_PRESETS } = await import('../frontend/src/settings.js');

const PRESET_FIELDS = [
  'name',
  'default_base_url',
  'default_chat_model',
  'preset_models',
  'description',
  'default_stt_model',
  'default_reasoning_effort',
];

test('provider_presets_v1 contract schema_version and non-empty preset list', () => {
  assert.equal(contract.schema_version, 'provider_presets_v1');
  assert.ok(Array.isArray(contract.presets) && contract.presets.length > 0);
});

test('BUILTIN_PRESETS matches provider_presets_v1 contract without drift', () => {
  const contractIds = contract.presets.map((p) => p.id);
  const builtinIds = BUILTIN_PRESETS.map((p) => p.id);

  assert.equal(new Set(contractIds).size, contractIds.length, 'contract preset ids must be unique');
  assert.equal(new Set(builtinIds).size, builtinIds.length, 'BUILTIN_PRESETS ids must be unique');
  assert.deepEqual(
    [...builtinIds].sort(),
    [...contractIds].sort(),
    'BUILTIN_PRESETS id set must match backend preset contract',
  );

  const builtinById = new Map(BUILTIN_PRESETS.map((p) => [p.id, p]));
  for (const expected of contract.presets) {
    const actual = builtinById.get(expected.id);
    assert.ok(actual, `missing BUILTIN_PRESETS entry for ${expected.id}`);
    for (const field of PRESET_FIELDS) {
      assert.deepEqual(
        actual[field],
        expected[field],
        `preset "${expected.id}" field "${field}" drifted from backend contract`,
      );
    }
    assert.deepEqual(actual, expected, `preset "${expected.id}" object drifted from backend contract`);
  }
});

test('ProviderSettingsController fallback presets populate identical defaults when /api/providers omits presets', async (t) => {
  const previousDocument = globalThis.document;
  const previousFetch = globalThis.fetch;
  const d = fakeDocument();
  globalThis.document = d;

  const toastRoot = d.createElement('div');
  toastRoot.id = 'toastRoot';
  d.body.appendChild(toastRoot);

  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ providers: [], presets: [] }),
  });

  const { createProviderSettingsController } = await import('../frontend/src/controllers/provider_settings.js');
  const dom = {
    gProvider: d.createElement('select'),
    gBaseUrl: d.createElement('input'),
    gChatModel: d.createElement('input'),
    gChatModelSelect: d.createElement('select'),
    gReasoningEffort: d.createElement('input'),
    gProviderDesc: d.createElement('div'),
    gApiKey: d.createElement('input'),
    gProviderTest: d.createElement('button'),
    gProviderActivate: d.createElement('button'),
  };
  const controller = createProviderSettingsController(dom, {});

  t.after(() => {
    controller.dispose();
    for (const toast of toastRoot.children) clearTimeout(toast._timer);
    globalThis.fetch = previousFetch;
    globalThis.document = previousDocument;
  });

  await controller.loadProviders();

  for (const expected of contract.presets) {
    dom.gProvider.value = expected.id;
    controller.onProviderChange();
    assert.equal(dom.gBaseUrl.value, expected.default_base_url, `${expected.id} default_base_url`);
    assert.equal(dom.gChatModel.value, expected.default_chat_model, `${expected.id} default_chat_model`);
    assert.equal(dom.gProviderDesc.textContent, expected.description, `${expected.id} description`);
    assert.equal(
      dom.gReasoningEffort.value,
      expected.default_reasoning_effort || '',
      `${expected.id} default_reasoning_effort`,
    );
    const modelOptionValues = dom.gChatModelSelect.children
      .map((opt) => opt.value)
      .filter((v) => v && v !== '__custom_model__');
    assert.deepEqual(modelOptionValues, expected.preset_models, `${expected.id} preset_models`);
  }
});

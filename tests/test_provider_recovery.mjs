import assert from 'node:assert/strict';
import test from 'node:test';
import { activateConfirmedProvider, saveConfirmedProvider } from '../frontend/src/controllers/provider_recovery.js';

const payload = { id: 'openai', name: 'OpenAI', api_base_url: 'https://api.openai.com/v1',
  chat_model: 'synthetic-model', reasoning_effort: null, is_active: true };
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });

function fixture(t, write, read = { ...payload, api_key: 'synt****key' }) {
  const original = globalThis.fetch, calls = [];
  t.after(() => { globalThis.fetch = original; });
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, ...options });
    if (options.method === 'POST') return write(url, options);
    if (read instanceof Error) throw read;
    return response({ provider: read });
  };
  return calls;
}

test('acknowledged provider writes need no verification request', async t => {
  const calls = fixture(t, () => response({ provider: { ...payload, api_key: 'synt****key' } }));
  const result = await saveConfirmedProvider({ ...payload, api_key: 'synthetic-secret-key' });
  assert.equal(result.provider.is_active, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, 'POST');
  assert.ok(!JSON.stringify(result).includes('synthetic-secret-key'));
});

for (const failure of ['network', 'timeout', 'unreadable-success']) {
  test(`lost provider response is checked once without replaying the write: ${failure}`, async t => {
    const calls = fixture(t, () => {
      if (failure === 'unreadable-success') return { ok: true, json: async () => { throw new SyntaxError(); } };
      if (failure === 'timeout') throw Object.assign(new Error('synthetic timeout'), { code: 'REQUEST_TIMEOUT' });
      throw new TypeError('Failed to fetch');
    });
    const result = await saveConfirmedProvider(payload);
    assert.equal(result.confirmed_after_disconnect, true);
    assert.equal(result.provider.chat_model, payload.chat_model);
    assert.deepEqual(calls.map(call => call.method || 'GET'), ['POST', 'GET']);
    assert.equal(calls[1].url, '/api/providers/openai');
    assert.equal(calls[1].cache, 'no-store');
    assert.equal(calls[1].body, undefined);
  });
}

for (const [field, value] of Object.entries({ id: 'other', name: 'Other name', chat_model: 'older-model',
  api_base_url: 'https://other.test/v1', reasoning_effort: 'high', is_active: false })) {
  test(`provider verification must not accept a different ${field}`, async t => {
    const calls = fixture(t, () => { throw new TypeError('Failed to fetch'); }, { ...payload, [field]: value });
    await assert.rejects(saveConfirmedProvider(payload), { code: 'PROVIDER_UNCONFIRMED' });
    assert.deepEqual(calls.map(call => call.method || 'GET'), ['POST', 'GET']);
  });
}

test('a colliding masked key cannot confirm a newly entered credential', async t => {
  const calls = fixture(t, () => { throw new TypeError('Failed to fetch'); }, { ...payload, api_key: 'same****tail' });
  await assert.rejects(saveConfirmedProvider({ ...payload, api_key: 'same-new-secret-tail' }),
    { code: 'PROVIDER_UNCONFIRMED' });
  assert.equal(calls.length, 1, 'do not read a mask as evidence about a new secret');
});

test('verification failure keeps the outcome unknown without retrying either request', async t => {
  const calls = fixture(t, () => { throw new TypeError('Failed to fetch'); }, new TypeError('Failed to fetch'));
  await assert.rejects(saveConfirmedProvider(payload), /保存结果尚未确认.*输入已保留/);
  assert.deepEqual(calls.map(call => call.method || 'GET'), ['POST', 'GET']);
});

test('provider verification does not treat an absent record as success', async t => {
  const calls = fixture(t, () => { throw new TypeError('Failed to fetch'); }, null);
  await assert.rejects(saveConfirmedProvider(payload), { code: 'PROVIDER_UNCONFIRMED' });
  assert.equal(calls.length, 2);
});

for (const status of [400, 401, 422, 500]) {
  test(`an explicit HTTP ${status} failure is retained without a read or retry`, async t => {
    const calls = fixture(t, () => response({ detail: '测试：配置未保存' }, status));
    await assert.rejects(saveConfirmedProvider(payload), { status });
    assert.equal(calls.length, 1);
  });
}

test('an explicit cancellation is retained without a read or retry', async t => {
  const calls = fixture(t, () => { throw new DOMException('操作已取消', 'AbortError'); });
  await assert.rejects(saveConfirmedProvider(payload), { name: 'AbortError' });
  assert.equal(calls.length, 1);
});

test('lost activation response verifies active fields after the save was acknowledged', async t => {
  const calls = fixture(t, () => { throw new TypeError('Failed to fetch'); });
  const result = await activateConfirmedProvider({ ...payload, api_key: 'already-acknowledged-secret' });
  assert.equal(result.active_provider.id, 'openai');
  assert.equal(result.confirmed_after_disconnect, true);
  assert.deepEqual(calls.map(call => call.method || 'GET'), ['POST', 'GET']);
  assert.equal(calls[0].url, '/api/providers/openai/activate');
  assert.equal(calls[1].body, undefined);
  assert.ok(!JSON.stringify(result).includes('already-acknowledged-secret'));
});

test('activation cannot be confirmed by a matching but inactive provider', async t => {
  const calls = fixture(t, () => { throw new TypeError('Failed to fetch'); }, { ...payload, is_active: false });
  const { is_active, ...fields } = payload;
  await assert.rejects(activateConfirmedProvider(fields), /启用结果尚未确认/);
  assert.equal(calls.length, 2);
});

test('activation cannot be confirmed when another model replaced the submitted one', async t => {
  fixture(t, () => { throw new TypeError('Failed to fetch'); }, { ...payload, chat_model: 'other-tab-model' });
  await assert.rejects(activateConfirmedProvider(payload), { code: 'PROVIDER_UNCONFIRMED' });
});

test('activation with an incomplete acknowledgement is verified before reporting success', async t => {
  const calls = fixture(t, () => response({ status: 'success' }));
  const result = await activateConfirmedProvider(payload);
  assert.equal(result.confirmed_after_disconnect, true);
  assert.equal(calls.length, 2);
});

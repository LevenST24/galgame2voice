import assert from 'node:assert/strict';
import { getModelStatus } from '../frontend/src/model_badge.js';

function render(provider, { profile = null, loaded = true, readFailed = false } = {}) {
  const callbacks = {
    getCurrentActiveProvider: () => provider,
    haveProvidersLoaded: () => loaded,
    hasProviderReadError: () => readFailed,
    getActive: () => ({ settings: { voiceProfileId: profile ? 7 : null } }),
    getVoiceProfiles: () => (profile ? [{ id: 7, name: profile }] : []),
  };
  const status = getModelStatus(callbacks);
  return { text: status.label, title: status.title, state: status.state };
}
let passed = 0;
{
  const failed = render(null, { readFailed: true });
  assert.match(failed.text, /无法读取/);
  assert.match(failed.title, /重试/);
  assert.equal(failed.state, 'error'); passed++;
}

{
  const ready = render({ id: 'gemini', name: 'Google Gemini', chat_model: 'gemini-x', api_key: '****abcd' });
  assert.equal(ready.text, 'gemini-x');
  assert.match(ready.title, /Google Gemini/);
  assert.equal(ready.state, 'ok'); passed++;
}
{
  const missing = render({ id: 'deepseek', name: 'DeepSeek', chat_model: 'deepseek-chat', api_key: '' }, { profile: '爱丽丝' });
  assert.equal(missing.text, '未填写 API Key');
  assert.match(missing.title, /爱丽丝/);
  assert.match(missing.title, /尚未填写 API Key/);
  assert.equal(missing.state, 'error'); passed++;
}
{
  // Local OpenAI-compatible services (Ollama, LM Studio) work without a key.
  const local = render({ id: 'custom', name: '本地模型', chat_model: 'qwen3', api_key: '' });
  assert.equal(local.text, 'qwen3');
  assert.equal(local.state, 'ok'); passed++;
}
{
  const none = render(null);
  assert.equal(none.text, '未配置对话模型');
  assert.match(none.title, /设置中心.*对话模型/); passed++;
}
{
  // Before the list arrives, "no active model" only means "not read yet".
  assert.equal(render(null, { loaded: false }).text, '正在读取模型配置…'); passed++;
}
console.log(`Model badge: ${passed} scenarios passed`);

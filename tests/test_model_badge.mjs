import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// Run the real header-badge renderer from main.js against a minimal DOM.
const main = readFileSync(new URL('../frontend/src/main.js', import.meta.url), 'utf8');
const start = main.indexOf('function updateBadge(');
const source = main.slice(start, main.indexOf('\n}', start) + 2);

function render(provider, { profile = null, loaded = true } = {}) {
  const label = { textContent: '' };
  const dom = { badgeDot: { className: '' }, badge: { title: '', querySelector: () => label } };
  const context = vm.createContext({
    dom,
    getCurrentActiveProvider: () => provider,
    haveProvidersLoaded: () => loaded,
    getActive: () => ({ settings: { voiceProfileId: profile ? 7 : null } }),
    getVoiceProfiles: () => (profile ? [{ id: 7, name: profile }] : []),
  });
  vm.runInContext(`${source}\nupdateBadge();`, context);
  return { text: label.textContent, title: dom.badge.title, dot: dom.badgeDot.className };
}

let passed = 0;
{
  const ready = render({ id: 'gemini', name: 'Google Gemini', chat_model: 'gemini-x', api_key: '****abcd' });
  assert.equal(ready.text, 'Google Gemini · gemini-x');
  assert.match(ready.dot, /dot-custom/); passed++;
}
{
  const missing = render({ id: 'deepseek', name: 'DeepSeek', chat_model: 'deepseek-chat', api_key: '' }, { profile: '爱丽丝' });
  assert.equal(missing.text, 'DeepSeek · 未填写 API Key · 🎙️ 爱丽丝');
  assert.match(missing.title, /尚未填写 API Key/);
  assert.match(missing.dot, /dot-mock/); passed++;
}
{
  // Local OpenAI-compatible services (Ollama, LM Studio) work without a key.
  const local = render({ id: 'custom', name: '本地模型', chat_model: 'qwen3', api_key: '' });
  assert.equal(local.text, '本地模型 · qwen3');
  assert.match(local.dot, /dot-custom/); passed++;
}
{
  const none = render(null);
  assert.equal(none.text, '未配置对话模型');
  assert.match(none.title, /全局设置/); passed++;
}
{
  // Before the list arrives, "no active model" only means "not read yet".
  assert.equal(render(null, { loaded: false }).text, '正在读取模型配置…'); passed++;
}
console.log(`Model badge: ${passed} scenarios passed`);

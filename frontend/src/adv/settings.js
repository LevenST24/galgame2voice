import { validAdvSetting, pickSettings } from '../session_data.js';

// Same field/label structure as the existing session panel. Values are inert text.
const fields = {
  advEnabled: ['启用 ADV 预览', ['false', 'true']],
  advPlayerName: ['玩家名字'],
  advPrimaryLanguage: ['主要语言', ['zh', 'ja']],
  advSubtitleLanguage: ['字幕语言', ['zh', 'ja', 'none']],
  advWindowOpacity: ['窗口不透明度', 0, 100],
  advTextIntervalMs: ['逐字间隔（毫秒）', 0, 100],
  advInterruptVoice: ['推进时打断语音', ['true', 'false']],
  advAutoBaseMs: ['AUTO 基础等待（毫秒）', 0, 6000],
  advAutoPerCharMs: ['AUTO 每字等待（毫秒）', 0, 200],
  advAutoMode: ['AUTO 模式', ['voice-priority', 'normal']],
  advOutfit: ['服装 ID（留空使用角色默认）'],
};
const labels = { true: '开启', false: '关闭', zh: '中文', ja: '日文', none: '无', 'voice-priority': '语音优先', normal: '通常' };

export function createAdvSettings(container, defaults) {
  if (!container) return { load() {}, read: () => ({}), snapshot: () => '' };
  const group = document.createElement('details');
  group.className = 'settings-advanced';
  const summary = document.createElement('summary');
  summary.textContent = 'ADV 预览（实验）';
  group.appendChild(summary);
  const body = document.createElement('div');
  body.className = 'settings-fields';
  group.appendChild(body);
  const controls = {};
  for (const [key, [label, options, max]] of Object.entries(fields)) {
    const field = document.createElement('div');
    field.className = 'field';
    const caption = document.createElement('label');
    caption.textContent = label;
    caption.htmlFor = `s-${key}`;
    field.appendChild(caption);
    const input = document.createElement(Array.isArray(options) ? 'select' : 'input');
    input.id = `s-${key}`;
    if (Array.isArray(options)) {
      for (const value of options) {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = labels[value];
        input.appendChild(option);
      }
    } else if (typeof options === 'number') {
      input.type = 'number'; input.min = options; input.max = max; input.step = 1;
    } else input.type = 'text';
    controls[key] = input;
    field.appendChild(input); body.appendChild(field);
  }
  // A small bilingual opacity preview uses no character assets or persisted URLs.
  const preview = document.createElement('div');
  preview.className = 'adv-settings-preview';
  preview.textContent = '【Player】你好，欢迎回来。\nこんにちは、おかえりなさい。';
  body.appendChild(preview);
  const updatePreview = () => {
    preview.style.backgroundColor = `rgba(12, 28, 58, ${Number(controls.advWindowOpacity.value) / 100})`;
  };
  controls.advWindowOpacity.addEventListener('input', updatePreview);
  controls.advPrimaryLanguage.addEventListener('change', () => {
    if (controls.advPrimaryLanguage.value === controls.advSubtitleLanguage.value) {
      controls.advSubtitleLanguage.value = controls.advPrimaryLanguage.value === 'zh' ? 'ja' : 'zh';
    }
  });
  container.appendChild(group);
  function load(settings) {
    const values = pickSettings(settings, defaults);
    for (const key of Object.keys(fields)) controls[key].value = String(values[key]);
    updatePreview();
  }
  function read() {
    const result = {};
    for (const key of Object.keys(fields)) {
      const raw = controls[key].value;
      if (typeof defaults[key] === 'boolean' && !['true', 'false'].includes(raw)) {
        throw new Error(`ADV 设置无效：${fields[key][0]}`);
      }
      const value = typeof defaults[key] === 'boolean' ? raw === 'true'
        : typeof defaults[key] === 'number' ? (raw.trim() ? Number(raw) : NaN) : raw;
      if (!validAdvSetting(key, value)) throw new Error(`ADV 设置无效：${fields[key][0]}`);
      result[key] = value;
    }
    if (result.advPrimaryLanguage === result.advSubtitleLanguage) throw new Error('主要语言和字幕语言需要不同。');
    return result;
  }
  load(defaults);
  return { load, read, snapshot: () => JSON.stringify(Object.values(controls).map(el => el.value)) };
}

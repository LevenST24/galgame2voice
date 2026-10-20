import { validAdvSetting, pickSettings } from '../session_data.js';

// Same field/label structure as the existing session panel. Values are inert text.
export const ADV_FIELDS = {
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
  advBlink: ['眨眼动画', ['true', 'false']],
  advLipSync: ['口型同步', ['true', 'false']],
};
export const ADV_LABELS = { true: '开启', false: '关闭', zh: '中文', ja: '日文', none: '无', 'voice-priority': '语音优先', normal: '通常' };

const fields = ADV_FIELDS;
const labels = ADV_LABELS;

export function validateAdvField(key, raw, defaults) {
  const meta = ADV_FIELDS[key];
  const label = meta ? meta[0] : key;
  if (typeof defaults?.[key] === 'boolean') {
    if (typeof raw === 'boolean') return { valid: true, value: raw };
    const str = String(raw ?? '');
    if (!['true', 'false'].includes(str)) {
      return { valid: false, error: `ADV 设置无效：${label}` };
    }
    return { valid: true, value: str === 'true' };
  }
  if (typeof defaults?.[key] === 'number') {
    const str = String(raw ?? '').trim();
    const num = str ? Number(str) : NaN;
    if (!Number.isFinite(num) || !validAdvSetting(key, num)) {
      return { valid: false, error: `ADV 设置无效：${label}` };
    }
    return { valid: true, value: num };
  }
  const str = String(raw ?? '');
  if (!validAdvSetting(key, str)) {
    return { valid: false, error: `ADV 设置无效：${label}` };
  }
  return { valid: true, value: str };
}

export function validateAdvLanguages(primary, subtitle) {
  if (primary && subtitle && primary === subtitle) {
    return '主要语言和字幕语言需要不同。';
  }
  return null;
}

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
      const parsed = validateAdvField(key, raw, defaults);
      if (!parsed.valid) throw new Error(parsed.error);
      result[key] = parsed.value;
    }
    const langError = validateAdvLanguages(result.advPrimaryLanguage, result.advSubtitleLanguage);
    if (langError) throw new Error(langError);
    return result;
  }
  load(defaults);
  return { load, read, snapshot: () => JSON.stringify(Object.values(controls).map(el => el.value)) };
}

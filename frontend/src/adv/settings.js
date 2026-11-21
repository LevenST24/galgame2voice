// @ts-check
import { validAdvSetting } from '../session_data.js';

// Same field/label structure as the existing session panel. Values are inert text.
const ADV_FIELDS = {
  advPlayerName: ['玩家名字'],
  advPrimaryLanguage: ['主要语言', ['zh', 'ja']],
  advSubtitleLanguage: ['字幕语言', ['zh', 'ja', 'none']],
  advWindowOpacity: ['窗口不透明度', 0, 100],
  advTextIntervalMs: ['逐字间隔（毫秒）', 0, 100],
  advInterruptVoice: ['推进时打断语音', ['true', 'false']],
  advAutoBaseMs: ['自动播放基础等待（毫秒）', 0, 6000],
  advAutoPerCharMs: ['自动播放每字等待（毫秒）', 0, 200],
  advAutoMode: ['自动播放模式', ['voice-priority', 'normal']],
  advOutfit: ['服装 ID（留空使用角色默认）'],
  advBlink: ['眨眼动画', ['true', 'false']],
  advLipSync: ['口型同步', ['true', 'false']],
};
export const ADV_LABELS = { true: '开启', false: '关闭', zh: '中文', ja: '日文', none: '无', 'voice-priority': '语音同步（推荐）', normal: '按阅读速度' };

export function validateAdvField(key, raw, defaults) {
  const meta = ADV_FIELDS[key];
  const label = meta ? meta[0] : key;
  if (typeof defaults?.[key] === 'boolean') {
    if (typeof raw === 'boolean') return { valid: true, value: raw };
    const str = String(raw ?? '');
    if (!['true', 'false'].includes(str)) {
      return { valid: false, error: `「${label}」填写有误，请重新输入。` };
    }
    return { valid: true, value: str === 'true' };
  }
  if (typeof defaults?.[key] === 'number') {
    const str = String(raw ?? '').trim();
    const num = str ? Number(str) : NaN;
    if (!Number.isFinite(num) || !validAdvSetting(key, num)) {
      return { valid: false, error: `「${label}」填写有误，请重新输入。` };
    }
    return { valid: true, value: num };
  }
  const str = String(raw ?? '');
  if (!validAdvSetting(key, str)) {
    return { valid: false, error: `「${label}」填写有误，请重新输入。` };
  }
  return { valid: true, value: str };
}

export function validateAdvLanguages(primary, subtitle) {
  if (primary && subtitle && primary === subtitle) {
    return '主要语言和字幕语言不能相同。';
  }
  return null;
}

// Voice Settings & Session Profile Controller
import { state, saveState, getActive, DEFAULT_SESSION_SETTINGS } from '../store.js';
import { portraitStage } from '../portrait.js';
import { showToast } from '../ui.js';

let _dom = {};
let _callbacks = {};
let voiceProfiles = [];
let activeProfileId = null;
let scannedModels = null;

export function initVoiceSettings(dom, callbacks = {}) {
  _dom = dom;
  _callbacks = callbacks;

  if (_dom.sVoice) {
    _dom.sVoice.addEventListener('change', syncCustomVoiceBox);
  }
  if (_dom.sVoiceDelete) {
    _dom.sVoiceDelete.addEventListener('click', deleteVoiceProfile);
  }
  if (_dom.sCvCreate) {
    _dom.sCvCreate.addEventListener('click', createCustomVoice);
  }

  // Sliders range input sync
  const sliders = [
    _dom.sTemp,
    _dom.sTopP,
    _dom.sFreq,
    _dom.sPres,
    _dom.sCtx,
    _dom.sTtsSpeed,
    _dom.sTtsTopK,
    _dom.sTtsTopP,
    _dom.sTtsTemp,
  ];
  sliders.forEach((r) => {
    if (r) r.addEventListener('input', syncRangeLabels);
  });

  if (_dom.sReset) {
    _dom.sReset.addEventListener('click', () => {
      _dom.sSystem.value = DEFAULT_SESSION_SETTINGS.systemPrompt;
      _dom.sVoice.value = '';
      _dom.sTemp.value = DEFAULT_SESSION_SETTINGS.temperature;
      _dom.sTopP.value = DEFAULT_SESSION_SETTINGS.topP;
      _dom.sFreq.value = DEFAULT_SESSION_SETTINGS.freqPenalty;
      _dom.sPres.value = DEFAULT_SESSION_SETTINGS.presPenalty;
      _dom.sMaxTokens.value = DEFAULT_SESSION_SETTINGS.maxTokens;
      _dom.sCtx.value = DEFAULT_SESSION_SETTINGS.maxContext;
      _dom.sAiAdaptiveVoice.value = String(DEFAULT_SESSION_SETTINGS.aiAdaptiveVoice !== false);
      _dom.sTtsSpeed.value = DEFAULT_SESSION_SETTINGS.ttsSpeed;
      _dom.sTtsTopK.value = DEFAULT_SESSION_SETTINGS.ttsTopK;
      _dom.sTtsTopP.value = DEFAULT_SESSION_SETTINGS.ttsTopP;
      _dom.sTtsTemp.value = DEFAULT_SESSION_SETTINGS.ttsTemperature;
      syncRangeLabels();
    });
  }

  if (_dom.sSave) {
    _dom.sSave.addEventListener('click', handleSaveSessionSettings);
  }

  if (_dom.sessionModal) {
    _dom.sessionModal.querySelectorAll('[data-close]').forEach((btn) => {
      btn.addEventListener('click', () => {
        if (_callbacks.closeModal) _callbacks.closeModal(_dom.sessionModal);
      });
    });
  }
}

export function getActiveProfileId() {
  return activeProfileId;
}

export function getVoiceProfiles() {
  return voiceProfiles;
}

export function openSessionSettings() {
  const s = getActive();
  if (!s) return;
  if (_dom.smTitle) _dom.smTitle.textContent = `· ${s.title}`;
  if (_dom.sSystem) _dom.sSystem.value = s.settings.systemPrompt;
  if (_dom.sTemp) _dom.sTemp.value = s.settings.temperature;
  if (_dom.sTopP) _dom.sTopP.value = s.settings.topP;
  if (_dom.sFreq) _dom.sFreq.value = s.settings.freqPenalty;
  if (_dom.sPres) _dom.sPres.value = s.settings.presPenalty;
  if (_dom.sMaxTokens) _dom.sMaxTokens.value = s.settings.maxTokens;
  if (_dom.sCtx) _dom.sCtx.value = s.settings.maxContext;
  if (_dom.sAiAdaptiveVoice) _dom.sAiAdaptiveVoice.value = String(s.settings.aiAdaptiveVoice !== false);
  if (_dom.sTtsSpeed) _dom.sTtsSpeed.value = s.settings.ttsSpeed;
  if (_dom.sTtsTopK) _dom.sTtsTopK.value = s.settings.ttsTopK;
  if (_dom.sTtsTopP) _dom.sTtsTopP.value = s.settings.ttsTopP;
  if (_dom.sTtsTemp) _dom.sTtsTemp.value = s.settings.ttsTemperature;
  syncRangeLabels();
  loadSessionVoiceSelect(s);
  if (_callbacks.openModal) _callbacks.openModal(_dom.sessionModal);
}

export function syncRangeLabels() {
  if (_dom.sTempVal && _dom.sTemp) _dom.sTempVal.textContent = Number(_dom.sTemp.value).toFixed(2);
  if (_dom.sTopPVal && _dom.sTopP) _dom.sTopPVal.textContent = Number(_dom.sTopP.value).toFixed(2);
  if (_dom.sFreqVal && _dom.sFreq) _dom.sFreqVal.textContent = Number(_dom.sFreq.value).toFixed(1);
  if (_dom.sPresVal && _dom.sPres) _dom.sPresVal.textContent = Number(_dom.sPres.value).toFixed(1);
  if (_dom.sCtxVal && _dom.sCtx) _dom.sCtxVal.textContent = _dom.sCtx.value;
  if (_dom.sTtsSpeedVal && _dom.sTtsSpeed) _dom.sTtsSpeedVal.textContent = Number(_dom.sTtsSpeed.value).toFixed(2);
  if (_dom.sTtsTopKVal && _dom.sTtsTopK) _dom.sTtsTopKVal.textContent = _dom.sTtsTopK.value;
  if (_dom.sTtsTopPVal && _dom.sTtsTopP) _dom.sTtsTopPVal.textContent = Number(_dom.sTtsTopP.value).toFixed(2);
  if (_dom.sTtsTempVal && _dom.sTtsTemp) _dom.sTtsTempVal.textContent = Number(_dom.sTtsTemp.value).toFixed(2);

  const sliders = [
    _dom.sTemp,
    _dom.sTopP,
    _dom.sFreq,
    _dom.sPres,
    _dom.sCtx,
    _dom.sTtsSpeed,
    _dom.sTtsTopK,
    _dom.sTtsTopP,
    _dom.sTtsTemp,
  ];
  for (const r of sliders) {
    if (r) {
      const pct = ((r.value - r.min) / (r.max - r.min)) * 100;
      r.style.setProperty('--fill', `${pct}%`);
    }
  }
}

export async function fetchVoiceProfiles() {
  const res = await fetch('/api/voice/profiles');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  voiceProfiles = data.profiles || [];
  activeProfileId = data.active_profile_id;
  return data;
}

export async function ensureSessionVoice(session, { silent = false } = {}) {
  const want = session?.settings?.voiceProfileId;
  if (!want) return;
  try {
    if (activeProfileId === null) await fetchVoiceProfiles();
    let res = await fetch('/api/voice/switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile_id: want }),
    });
    let data = await res.json().catch(() => ({}));
    if (!res.ok && res.status === 503) {
      res = await fetch('/api/voice/switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile_id: want, force: true }),
      });
      data = await res.json().catch(() => ({}));
    }
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    activeProfileId = want;
    if (data && data.profile) {
      try {
        portraitStage.setCharacter(data.profile, want);
      } catch (_) {}
    }
    if (!silent) showToast(`已切换音色：${data.profile || want}`, 'success');
  } catch (e) {
    showToast(`音色切换失败: ${e.message || e}`, 'error');
  }
}

export async function loadSessionVoiceSelect(session) {
  if (!_dom.sVoice) return;
  _dom.sVoice.innerHTML = '<option value="">跟随后端当前音色</option>';
  if (_dom.sVoiceActive) _dom.sVoiceActive.textContent = '';
  try {
    await fetchVoiceProfiles();
    const active = voiceProfiles.find((p) => p.id === activeProfileId);
    if (_dom.sVoiceActive) {
      _dom.sVoiceActive.textContent = active ? `当前加载：${active.name}` : '';
    }
    const sortedProfiles = active ? [active, ...voiceProfiles.filter((p) => p !== active)] : voiceProfiles;
    for (const p of sortedProfiles) {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = `${p.name}${p.id === activeProfileId ? '（当前加载）' : ''}`;
      _dom.sVoice.appendChild(opt);
    }
    const customOpt = document.createElement('option');
    customOpt.value = '__custom__';
    customOpt.textContent = '＋ 新建自定义音色（选择 ckpt / pth / 参考音频）…';
    _dom.sVoice.appendChild(customOpt);
    if (session?.settings?.voiceProfileId) {
      _dom.sVoice.value = String(session.settings.voiceProfileId);
    }
  } catch (e) {
    _dom.sVoice.innerHTML = '<option value="">加载失败（后端未启动？）</option>';
  }
  syncCustomVoiceBox();
}

export function syncCustomVoiceBox() {
  if (!_dom.sVoice || !_dom.sCustomVoiceBox) return;
  const show = _dom.sVoice.value === '__custom__';
  _dom.sCustomVoiceBox.classList.toggle('hidden', !show);
  if (show) populateScanOptions();
}

export async function populateScanOptions() {
  const fill = (sel, list, emptyHint) => {
    if (!sel) return;
    sel.innerHTML = list.length
      ? list.map((f) => `<option value="${f.path}">${f.name}</option>`).join('')
      : `<option value="">${emptyHint}</option>`;
  };
  try {
    if (!scannedModels) {
      const res = await fetch('/api/voice/scan-models');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      scannedModels = await res.json();
    }
    fill(_dom.sCvGpt, scannedModels.gpt_weights || [], '（未扫描到 .ckpt 文件）');
    fill(_dom.sCvSovits, scannedModels.sovits_weights || [], '（未扫描到 .pth 文件）');
    fill(_dom.sCvRef, scannedModels.audio_files || [], '（未扫描到音频文件，可留空音色但质量差）');
  } catch (e) {
    [_dom.sCvGpt, _dom.sCvSovits, _dom.sCvRef].forEach((sel) => {
      if (sel) sel.innerHTML = `<option value="">扫描失败（${e.message || e}）</option>`;
    });
  }
}

export async function createCustomVoice() {
  const name = _dom.sCvName.value.trim();
  const gptPath = _dom.sCvGpt.value;
  const sovitsPath = _dom.sCvSovits.value;
  const refPath = _dom.sCvRef.value;
  const promptText = _dom.sCvPromptText.value.trim();
  if (!name || !gptPath || !sovitsPath) {
    showToast('请填写音色名称并选择 GPT / SoVITS 模型文件', 'error');
    return;
  }
  if (!refPath) {
    showToast('必须选择参考音频：没有声音样本，合成音色会严重失真', 'error');
    return;
  }
  if (!promptText) {
    showToast('必须填写参考音频里说的话：SoVITS V3/V4 模型留空会直接导致合成失败（400）', 'error');
    return;
  }
  _dom.sCvCreate.disabled = true;
  try {
    const payload = {
      name,
      gpt_weights_path: gptPath,
      sovits_weights_path: sovitsPath,
      ref_audio_path: refPath,
      prompt_text: promptText,
      prompt_lang: _dom.sCvLang.value,
      text_lang: _dom.sCvLang.value,
    };
    const res = await fetch('/api/voice/profiles', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    const profileId = data.id;
    const s = getActive();
    if (s) {
      s.settings.voiceProfileId = profileId;
      saveState();
    }
    scannedModels = null;
    await loadSessionVoiceSelect(s);
    _dom.sVoice.value = String(profileId);
    syncCustomVoiceBox();
    showToast(`音色「${data.name}」已创建并绑定到当前会话`, 'success');
    ensureSessionVoice(getActive());
  } catch (e) {
    showToast(`音色创建失败: ${e.message || e}`, 'error');
  } finally {
    _dom.sCvCreate.disabled = false;
  }
}

export async function deleteVoiceProfile() {
  const id = Number(_dom.sVoice.value);
  if (!id) {
    showToast('请先在下拉框中选择要删除的音色', 'error');
    return;
  }
  if (id === activeProfileId) {
    showToast('该音色正在被引擎加载，请先把会话切换到其他音色再删除', 'error');
    return;
  }
  const p = voiceProfiles.find((x) => x.id === id);
  if (!window.confirm(`确定删除音色「${p ? p.name : id}」？此操作不可恢复。`)) return;
  _dom.sVoiceDelete.disabled = true;
  try {
    const res = await fetch(`/api/voice/profiles/${id}`, { method: 'DELETE' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    let cleared = 0;
    for (const sess of state.sessions) {
      if (sess.settings?.voiceProfileId === id) {
        sess.settings.voiceProfileId = null;
        cleared += 1;
      }
    }
    if (cleared) saveState();
    await loadSessionVoiceSelect(getActive());
    showToast(`音色「${p ? p.name : id}」已删除${cleared ? `，并解除了 ${cleared} 个会话的绑定` : ''}`, 'success');
  } catch (e) {
    showToast(`删除失败: ${e.message || e}`, 'error');
  } finally {
    _dom.sVoiceDelete.disabled = false;
  }
}

async function handleSaveSessionSettings() {
  const s = getActive();
  if (!s) return;
  const maxTokens = Math.round(Number(_dom.sMaxTokens.value)) || DEFAULT_SESSION_SETTINGS.maxTokens;
  s.settings = {
    systemPrompt: _dom.sSystem.value,
    voiceProfileId: _dom.sVoice.value && _dom.sVoice.value !== '__custom__' ? Number(_dom.sVoice.value) : null,
    temperature: Number(_dom.sTemp.value),
    topP: Number(_dom.sTopP.value),
    maxTokens: Math.min(32768, Math.max(16, maxTokens)),
    freqPenalty: Number(_dom.sFreq.value),
    presPenalty: Number(_dom.sPres.value),
    maxContext: Number(_dom.sCtx.value),
    aiAdaptiveVoice: _dom.sAiAdaptiveVoice.value === 'true',
    ttsSpeed: Number(_dom.sTtsSpeed.value),
    ttsTopK: Math.round(Number(_dom.sTtsTopK.value)),
    ttsTopP: Number(_dom.sTtsTopP.value),
    ttsTemperature: Number(_dom.sTtsTemp.value),
  };
  s.updatedAt = Date.now();
  saveState();

  try {
    const res = await fetch('/api/chat/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: s.id,
        title: s.title,
        voice_profile_id: s.settings?.voiceProfileId,
        custom_system_prompt: s.settings?.systemPrompt,
        settings: s.settings,
      }),
    });
    if (!res.ok) {
      console.warn('Failed to persist session to backend:', res.status);
    }
  } catch (err) {
    console.warn('Network error persisting session to backend:', err);
  }

  if (_callbacks.closeModal) _callbacks.closeModal(_dom.sessionModal);
  if (_callbacks.renderSidebar) _callbacks.renderSidebar();
  if (_callbacks.renderHeader) _callbacks.renderHeader();
  await ensureSessionVoice(s);
  showToast('会话参数已保存', 'success');
}

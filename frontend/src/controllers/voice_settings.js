// Voice Settings & Session Profile Controller
import { state, saveState, getActive, DEFAULT_SESSION_SETTINGS } from '../store.js';
import { showToast } from '../ui.js';
import { requestJson } from '../api.js';
import { persistSession } from '../session_api.js';
import { createAdvSettings } from '../adv/settings.js';

let _dom = {};
let _callbacks = {};
let advSettings = { load() {}, read: () => ({}), snapshot: () => '' };
let voiceProfiles = [];
// 面板里人设框的初始值与对应角色，用于换角色时判断"有未保存的编辑"
let _promptBaseline = '';
let _promptOwnerId = null;
let _promptSelection = '';
let editorRevision = 0;
let activeProfileId = null;
let scannedModels = null;
let pendingScan = null;
let scanRevision = 0;
let sessionVoiceRevision = 0;
let voiceSelectRevision = 0;
// Value of the only option left when the voice list fails to load. It is not a choice:
// saving keeps the session's binding instead of treating it as "no character".
const VOICE_LIST_UNAVAILABLE = '__unavailable__';
let profilesRevision = 0;

export function invalidateScannedModels() {
  scannedModels = null;
  pendingScan = null;
  scanRevision += 1;
}

export function initVoiceSettings(dom, callbacks = {}) {
  _dom = dom;
  _callbacks = callbacks;
  advSettings = createAdvSettings(_dom.sessionModal?.querySelector('.modal-body'), DEFAULT_SESSION_SETTINGS);
  editorRevision += 1;

  if (_dom.sVoice) {
    _dom.sVoice.addEventListener('change', syncSystemPromptToSelectedVoice);
    _dom.sVoice.addEventListener('change', syncCustomVoiceBox);
  }
  if (_dom.sVoiceRescan) {
    _dom.sVoiceRescan.addEventListener('click', handleRescanCharacters);
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
      // 人设提示词属于角色包、不属于会话参数，重置只恢复推理参数，不抹人设
      _dom.sTemp.value = DEFAULT_SESSION_SETTINGS.temperature;
      _dom.sTopP.value = DEFAULT_SESSION_SETTINGS.topP;
      _dom.sFreq.value = DEFAULT_SESSION_SETTINGS.freqPenalty;
      _dom.sPres.value = DEFAULT_SESSION_SETTINGS.presPenalty;
      _dom.sMaxTokens.value = DEFAULT_SESSION_SETTINGS.maxTokens;
      _dom.sCtx.value = DEFAULT_SESSION_SETTINGS.maxContext;
      _dom.sAiAdaptiveVoice.value = String(DEFAULT_SESSION_SETTINGS.aiAdaptiveVoice !== false);
      _dom.sAdvReplyFormat.value = DEFAULT_SESSION_SETTINGS.advReplyFormat;
      _dom.sTtsSpeed.value = DEFAULT_SESSION_SETTINGS.ttsSpeed;
      _dom.sTtsTopK.value = DEFAULT_SESSION_SETTINGS.ttsTopK;
      _dom.sTtsTopP.value = DEFAULT_SESSION_SETTINGS.ttsTopP;
      _dom.sTtsTemp.value = DEFAULT_SESSION_SETTINGS.ttsTemperature;
      advSettings.load(DEFAULT_SESSION_SETTINGS);
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

export async function openSessionSettings() {
  const s = getActive();
  if (!s) return;
  const revision = ++editorRevision;
  if (_dom.smTitle) _dom.smTitle.textContent = `· ${s.title}`;
  if (_dom.sSystem) {
    // 人设提示词是角色级数据，权威源在角色包；这里只展示当前绑定角色的那一份
    if (!voiceProfiles.length) {
      try { await fetchVoiceProfiles(); } catch (_) { /* 下面按空值处理 */ }
    }
    if (getActive() !== s || revision !== editorRevision) return;
    const prof = voiceProfiles.find((p) => p.id === s.settings?.voiceProfileId);
    applyProfilePromptToField(prof ? prof.id : null);
  }
  if (_dom.sTemp) _dom.sTemp.value = s.settings.temperature;
  if (_dom.sTopP) _dom.sTopP.value = s.settings.topP;
  if (_dom.sFreq) _dom.sFreq.value = s.settings.freqPenalty;
  if (_dom.sPres) _dom.sPres.value = s.settings.presPenalty;
  if (_dom.sMaxTokens) _dom.sMaxTokens.value = s.settings.maxTokens;
  if (_dom.sCtx) _dom.sCtx.value = s.settings.maxContext;
  if (_dom.sAiAdaptiveVoice) _dom.sAiAdaptiveVoice.value = String(s.settings.aiAdaptiveVoice !== false);
  if (_dom.sAdvReplyFormat) _dom.sAdvReplyFormat.value = s.settings.advReplyFormat === 'lines' ? 'lines' : 'old';
  if (_dom.sTtsSpeed) _dom.sTtsSpeed.value = s.settings.ttsSpeed;
  if (_dom.sTtsTopK) _dom.sTtsTopK.value = s.settings.ttsTopK;
  if (_dom.sTtsTopP) _dom.sTtsTopP.value = s.settings.ttsTopP;
  if (_dom.sTtsTemp) _dom.sTtsTemp.value = s.settings.ttsTemperature;
  advSettings.load(s.settings);
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
  const revision = ++profilesRevision;
  const data = await requestJson('/api/voice/profiles');
  if (revision === profilesRevision) {
    voiceProfiles = data.profiles || [];
    activeProfileId = data.active_profile_id;
  }
  return data;
}

export async function ensureSessionVoice(session, { silent = false } = {}) {
  const revision = ++sessionVoiceRevision;
  const want = session?.settings?.voiceProfileId;
  const isCurrent = () => revision === sessionVoiceRevision
    && (!session?.id || getActive()?.id === session.id)
    && session?.settings?.voiceProfileId === want;
  if (activeProfileId === null) {
    try {
      await fetchVoiceProfiles();
      if (!isCurrent()) return;
      _callbacks.renderHeader?.();
    } catch { /* Text and ADV art remain available without the voice service. */ }
  }
  if (!want) return;
  try {
    if (activeProfileId === null) await fetchVoiceProfiles();
    if (!isCurrent()) return;
    const data = await requestJson('/api/voice/switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile_id: want }),
    }, { timeoutMs: 120000 });
    if (!isCurrent()) return;
    activeProfileId = want;
    // 换人后若面板正开着且人设框没有未保存编辑，就跟着换成新角色的那一份
    const modalOpen = _dom.sessionModal && !_dom.sessionModal.classList.contains('hidden');
    if (modalOpen && _dom.sSystem) {
      const clean = (_dom.sSystem.value || '').trim() === _promptBaseline.trim();
      if (clean && String(_dom.sVoice?.value) === String(want)) applyProfilePromptToField(want);
    }
    if (_callbacks.renderHeader) _callbacks.renderHeader();
    if (!silent) showToast(`已切换音色：${data.profile || want}`, 'success');
  } catch (e) {
    if (!isCurrent()) return;
    if (e.status === 404) {
      try {
        await fetchVoiceProfiles();
        if (!isCurrent()) return;
        if (!voiceProfiles.some(profile => profile.id === want)
          && session?.settings?.voiceProfileId === want) {
          session.settings.voiceProfileId = null;
          _callbacks.invalidateSessionSync?.();
          saveState();
          // The server copy still names the deleted voice; the next sync would restore it.
          if (state.sessions.includes(session)) persistSession(session).catch(() => {});
          _callbacks.renderHeader?.();
          showToast('这个会话绑定的音色已被删除，已解除失效绑定。文字聊天可继续；请在「会话设置」选择或创建新音色。', 'info', _callbacks.onOpenSessionSettings ? {
            actionLabel: '选择音色',
            onAction: () => _callbacks.onOpenSessionSettings(),
          } : null);
          return;
        }
      } catch { /* Keep the binding when its existence cannot be checked. */ }
    }
    showToast(`音色切换失败: ${e.message || e}`, 'error', _callbacks.onOpenEngineStatus ? {
      actionLabel: '检查语音状态',
      onAction: () => _callbacks.onOpenEngineStatus(),
    } : null);
  }
}

export async function loadSessionVoiceSelect(session, { preserveDraft = false } = {}) {
  if (!_dom.sVoice) return;
  const revision = ++voiceSelectRevision;
  const editor = editorRevision;
  const promptAtStart = _dom.sSystem?.value;
  const ownerAtStart = _promptOwnerId;
  const isCurrent = () => revision === voiceSelectRevision && editor === editorRevision
    && (!session?.id || getActive() === session);
  try {
    await fetchVoiceProfiles();
    if (!isCurrent()) return;
    // Read the latest selection after the request: the user may keep editing
    // while a rescan is running. Refresh options without changing its owner.
    const keepDraft = preserveDraft || _dom.sSystem?.value !== promptAtStart || _promptOwnerId !== ownerAtStart;
    let draftSelection = keepDraft ? String(_dom.sVoice.value || '') : null;
    // A placeholder left by an earlier failed load is not a user choice.
    if (draftSelection === VOICE_LIST_UNAVAILABLE) draftSelection = String(session?.settings?.voiceProfileId || '');
    _dom.sVoice.innerHTML = '<option value="">跟随后端当前音色</option>';
    const active = voiceProfiles.find((p) => p.id === activeProfileId);
    if (_dom.sVoiceActive) {
      _dom.sVoiceActive.textContent = active ? `当前加载：${active.name}`
        : (!voiceProfiles.length ? '首次使用：请选择模型权重和参考音频，创建会话音色。' : '');
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
    if (keepDraft) {
      if (draftSelection && draftSelection !== '__custom__'
          && !voiceProfiles.some(p => String(p.id) === draftSelection)) {
        const missingOpt = document.createElement('option');
        missingOpt.value = draftSelection;
        missingOpt.textContent = '原角色已不可用（人设草稿已保留，请重新选择角色）';
        missingOpt.disabled = true;
        _dom.sVoice.appendChild(missingOpt);
      }
      _dom.sVoice.value = draftSelection;
    } else if (!voiceProfiles.length) {
      _dom.sVoice.value = '__custom__';
    } else if (session?.settings?.voiceProfileId) {
      _dom.sVoice.value = String(session.settings.voiceProfileId);
    }
    const selVal = _dom.sVoice.value;
    const selectedId = selVal && selVal !== '__custom__' ? Number(selVal) : null;
    const cleanOwnedProfile = selectedId && _promptOwnerId === selectedId
      && (_dom.sSystem?.value || '').trim() === _promptBaseline.trim()
      && voiceProfiles.some(p => p.id === selectedId);
    // A clean editor follows package updates; only an unsaved edit must retain
    // its old text. Otherwise Save could undo a newly scanned package prompt.
    if (!keepDraft || cleanOwnedProfile) {
      applyProfilePromptToField(
        selectedId,
        { isCustomNew: selVal === '__custom__' }
      );
    }
  } catch (e) {
    if (!isCurrent()) return;
    if (!preserveDraft) {
      _dom.sVoice.innerHTML = `<option value="${VOICE_LIST_UNAVAILABLE}">加载失败（后端未启动？）</option>`;
      _dom.sVoice.value = VOICE_LIST_UNAVAILABLE;
    } else {
      // Let the rescan report a failed refresh rather than claiming success.
      throw e;
    }
  }
  syncCustomVoiceBox();
}

/**
 * 面板里换角色时，人设框立刻换成新角色的那一份。
 * 框里若有未保存的编辑先确认并允许回退选择 —— 否则保存时会把上一个角色的
 * 文字写进新角色的包里，那是静默的数据损坏。
 */
function syncSystemPromptToSelectedVoice() {
  if (!_dom.sSystem || !_dom.sVoice) return;
  const raw = _dom.sVoice.value;
  const isCustomNew = raw === '__custom__';
  const id = raw && !isCustomNew ? Number(raw) : null;
  if (raw === _promptSelection) return;
  const current = (_dom.sSystem.value || '').trim();
  if (current !== _promptBaseline.trim()
      && !window.confirm('人设提示词有未保存的修改，切换角色会覆盖它。仍要切换吗？')) {
    _dom.sVoice.value = _promptSelection;
    return;
  }
  applyProfilePromptToField(id, { isCustomNew });
}

/** 把指定角色的人设提示词填进人设框，并记录基线（不改动角色数据本身）。 */
function applyProfilePromptToField(profileId, { isCustomNew = false } = {}) {
  const prof = voiceProfiles.find((p) => p.id === profileId);
  const text = (prof && prof.system_prompt) || '';
  _promptBaseline = text;
  _promptOwnerId = profileId ?? null;
  _promptSelection = isCustomNew ? '__custom__' : profileId == null ? '' : String(profileId);
  if (_dom.sSystem) {
    _dom.sSystem.value = text;
    _dom.sSystem.disabled = !prof && !isCustomNew;
    _dom.sSystem.placeholder = prof
      ? `编辑「${prof.name}」的人设提示词，保存后写回角色包并对该角色的所有会话生效…`
      : (isCustomNew
        ? '可选：在此填写新建自定义音色的人设提示词，创建时将自动保存…'
        : '当前会话未绑定角色，请先在下方选择角色音色后再编辑人设提示词…');
  }
}

export function syncCustomVoiceBox() {
  if (!_dom.sVoice || !_dom.sCustomVoiceBox) return;
  const show = _dom.sVoice.value === '__custom__';
  _dom.sCustomVoiceBox.classList.toggle('hidden', !show);
  if (show) populateScanOptions();
}

export async function populateScanOptions() {
  if (pendingScan) return pendingScan;
  const revision = scanRevision;
  const dom = _dom;
  const pending = refreshScanOptions(revision, dom);
  pendingScan = pending;
  try { await pending; }
  finally { if (pendingScan === pending) pendingScan = null; }
}

async function refreshScanOptions(revision, dom) {
  const isCurrent = () => revision === scanRevision && dom === _dom;
  const fill = (sel, list, emptyHint) => {
    if (!sel) return;
    const previous = sel.value;
    const stillAvailable = list.some(file => file.path === previous);
    const needsChoice = !stillAvailable && (previous || (sel.options || sel.children).length > 0);
    sel.innerHTML = '';
    if (!list.length || needsChoice) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = previous && !stillAvailable ? '（原选文件未扫描到，请重新选择）'
        : list.length ? '（请选择模型或参考音频文件）' : emptyHint;
      sel.appendChild(opt);
    }
    for (const file of list) {
      const opt = document.createElement('option');
      opt.value = file.path;
      opt.textContent = file.name;
      sel.appendChild(opt);
    }
    sel.value = stillAvailable ? previous : needsChoice ? '' : list[0]?.path || '';
  };
  try {
    if (!scannedModels) {
      const models = await requestJson('/api/voice/scan-models', {}, { timeoutMs: 60000 });
      if (!isCurrent()) return;
      scannedModels = models;
    }
    fill(dom.sCvGpt, scannedModels.gpt_weights || [], '（未扫描到 .ckpt 文件；请先在「全局设置 → 语音服务」选择引擎目录）');
    fill(dom.sCvSovits, scannedModels.sovits_weights || [], '（未扫描到 .pth 文件；请先在「全局设置 → 语音服务」选择引擎目录）');
    fill(dom.sCvRef, scannedModels.audio_files || [], '（未扫描到音频文件；参考音频为必选项，无法创建自定义音色）');
  } catch (e) {
    if (!isCurrent()) return;
    [dom.sCvGpt, dom.sCvSovits, dom.sCvRef].forEach((sel) => {
      if (sel && !(sel.options || sel.children).length) {
        sel.innerHTML = '';
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = `扫描失败（${e.message || e}）`;
        sel.appendChild(opt);
      }
    });
    showToast(`模型扫描失败，已有选择已保留；可点击音色旁的「扫描」重试：${e.message || e}`, 'error');
  }
}

export async function createCustomVoice() {
  if (_dom.sCvCreate.disabled) return;
  const origin = getActive();
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
    const customSystemPrompt = (_dom.sSystem?.value || '').trim();
    const payload = {
      name,
      gpt_weights_path: gptPath,
      sovits_weights_path: sovitsPath,
      ref_audio_path: refPath,
      prompt_text: promptText,
      prompt_lang: _dom.sCvLang.value,
      text_lang: _dom.sCvLang.value,
      system_prompt: customSystemPrompt,
    };
    const data = await requestJson('/api/voice/profiles', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const profileId = data.id;
    let note = '';
    if (origin && state.sessions.includes(origin)) {
      _callbacks.invalidateSessionSync?.();
      origin.settings.voiceProfileId = profileId;
      saveState();
      try { await persistSession(origin); }
      catch (e) { note = `；会话绑定尚未同步到本机服务：${e.message}`; }
    }
    invalidateScannedModels();
    if (getActive() === origin && state.sessions.includes(origin)) {
      await loadSessionVoiceSelect(origin);
      if (getActive() === origin) {
        _dom.sVoice.value = String(profileId);
        syncCustomVoiceBox();
        ensureSessionVoice(origin);
      }
    }
    const binding = state.sessions.includes(origin) ? `，已绑定到「${origin.title}」` : '，可在会话设置中选择';
    showToast(`音色「${data.name}」已创建${binding}${note}`, note ? 'info' : 'success');
  } catch (e) {
    showToast(`音色创建失败: ${e.message || e}`, 'error');
  } finally {
    _dom.sCvCreate.disabled = false;
  }
}

export async function handleRescanCharacters() {
  if (_dom.sVoiceRescan?.disabled) return;
  if (_dom.sVoiceRescan) _dom.sVoiceRescan.disabled = true;
  try {
    const res = await requestJson('/api/characters/rescan', { method: 'POST' }, { timeoutMs: 30000 });
    invalidateScannedModels();
    await loadSessionVoiceSelect(getActive(), { preserveDraft: true });
    if (_dom.sVoice?.value === '__custom__') {
      await populateScanOptions();
    }
    const count = res?.discovered_count ?? 0;
    showToast(count > 0 ? `已扫描并同步 ${count} 个角色包` : '已完成扫描（未发现新的角色包文件夹）', 'success');
  } catch (e) {
    showToast(`扫描角色包失败: ${e.message || e}`, 'error');
  } finally {
    if (_dom.sVoiceRescan) _dom.sVoiceRescan.disabled = false;
  }
}

export async function deleteVoiceProfile() {
  if (_dom.sVoiceDelete.disabled) return;
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
    await requestJson(`/api/voice/profiles/${id}`, { method: 'DELETE' });
    const cleared = state.sessions.filter(sess => sess.settings?.voiceProfileId === id);
    for (const sess of cleared) sess.settings.voiceProfileId = null;
    if (cleared.length) {
      _callbacks.invalidateSessionSync?.();
      saveState();
      // The server copies still name the deleted voice; the next sync would restore them.
      for (const sess of cleared) persistSession(sess).catch(() => {});
    }
    await loadSessionVoiceSelect(getActive());
    ensureSessionVoice(getActive(), { silent: true });
    showToast(`音色「${p ? p.name : id}」已删除${cleared.length ? `，并解除了 ${cleared.length} 个会话的绑定` : ''}`, 'success');
  } catch (e) {
    showToast(`删除失败: ${e.message || e}`, 'error');
  } finally {
    _dom.sVoiceDelete.disabled = false;
  }
}

const EDITOR_FIELDS = [
  'sVoice', 'sSystem', 'sTemp', 'sTopP', 'sMaxTokens', 'sFreq', 'sPres', 'sCtx',
  'sAiAdaptiveVoice', 'sAdvReplyFormat', 'sTtsSpeed', 'sTtsTopK', 'sTtsTopP', 'sTtsTemp',
  'sCvName', 'sCvGpt', 'sCvSovits', 'sCvRef', 'sCvPromptText', 'sCvLang',
];
const editorSnapshot = () => JSON.stringify([EDITOR_FIELDS.map(key => _dom[key]?.value), advSettings.snapshot()]);

async function handleSaveSessionSettings() {
  if (_dom.sSave?.disabled) return;
  const s = getActive();
  if (!s) return;
  const editor = editorRevision;
  const submitted = editorSnapshot();
  const sameEditor = () => getActive() === s && editor === editorRevision;
  if (_dom.sSave) _dom.sSave.disabled = true;
  try {
    let advValues;
    try { advValues = advSettings.read(); }
    catch (error) { showToast(error.message, 'error'); return; }
    if (_dom.sVoice.value === '__custom__') {
      showToast('请先点击「创建并绑定」完成新音色的创建，再保存会话参数。填写的内容已保留。', 'info');
      return;
    }
    _callbacks.invalidateSessionSync?.();
    const maxTokens = Math.round(Number(_dom.sMaxTokens.value)) || DEFAULT_SESSION_SETTINGS.maxTokens;
    const voiceListUnavailable = _dom.sVoice.value === VOICE_LIST_UNAVAILABLE;
    let profileId = _dom.sVoice.value && _dom.sVoice.value !== '__custom__'
      ? Number(_dom.sVoice.value)
      : null;
    if (voiceListUnavailable) profileId = s.settings?.voiceProfileId ?? null;
    if (!voiceListUnavailable && profileId
        && (_promptOwnerId !== profileId || !voiceProfiles.some(p => p.id === profileId))) {
      showToast('人设草稿与当前角色不一致，已保留编辑内容。请重新选择对应角色后再保存。', 'error');
      return;
    }
    s.settings = {
      ...s.settings,
      ...advValues,
      voiceProfileId: profileId,
      temperature: Number(_dom.sTemp.value),
      topP: Number(_dom.sTopP.value),
      maxTokens: Math.min(32768, Math.max(16, maxTokens)),
      freqPenalty: Number(_dom.sFreq.value),
      presPenalty: Number(_dom.sPres.value),
      maxContext: Number(_dom.sCtx.value),
      aiAdaptiveVoice: _dom.sAiAdaptiveVoice.value === 'true',
      advReplyFormat: _dom.sAdvReplyFormat.value === 'lines' ? 'lines' : 'old',
      ttsSpeed: Number(_dom.sTtsSpeed.value),
      ttsTopK: Math.round(Number(_dom.sTtsTopK.value)),
      ttsTopP: Number(_dom.sTtsTopP.value),
      ttsTemperature: Number(_dom.sTtsTemp.value),
    };
    s.updatedAt = Date.now();
    saveState();

    // 人设提示词属于角色、不属于会话：写回角色包 manifest.json，DB 镜像由后端刷新
    const { note: promptNote, confirmed: promptConfirmed } = voiceListUnavailable
      ? { note: '；角色列表未能加载，角色绑定与人设保持不变', confirmed: false }
      : await saveCharacterPrompt(profileId, editor);

    let syncNote = '';
    try {
      // The session may have been deleted while its character prompt was saving.
      if (state.sessions.includes(s)) await persistSession(s);
    } catch (err) {
      syncNote = `；本机服务尚未确认保存：${err.message}`;
    }

    if (sameEditor() && editorSnapshot() === submitted && promptConfirmed && !syncNote && _callbacks.closeModal) {
      _callbacks.closeModal(_dom.sessionModal);
    }
    if (_callbacks.renderSidebar) _callbacks.renderSidebar();
    if (_callbacks.renderHeader) _callbacks.renderHeader();
          if (getActive() === s) await ensureSessionVoice(s);
    const draftNote = sameEditor() && editorSnapshot() !== submitted ? '；保存期间新增的修改已保留在面板中，请再次保存' : '';
    showToast(`会话「${s.title}」参数已更新${promptNote}${syncNote}${draftNote}`, promptNote || syncNote || draftNote ? 'info' : 'success');
  } finally {
    if (_dom.sSave) _dom.sSave.disabled = false;
  }
}

/** 写回角色包；失败时保留面板中的编辑内容，供用户检查和重试。 */
async function saveCharacterPrompt(profileId, editor) {
  const result = (note = '', confirmed = true) => ({ note, confirmed });
  if (!_dom.sSystem) return result();
  if (!profileId) {
    return (_dom.sSystem.value || '').trim()
      ? result('；当前会话未绑定角色，人设改动未保存', false) : result();
  }
  const prof = voiceProfiles.find((p) => p.id === profileId);
  const text = (_dom.sSystem.value || '').trim();
  if (_promptOwnerId !== profileId) return result('；人设草稿属于另一个角色，未写入角色包', false);
  if (!prof) return result('；找不到对应角色包，人设改动未保存', false);
  // Unchanged first: a character created without a persona must still save its session.
  if (text === (prof.system_prompt || '').trim()) return result();
  if (!text) return result('；人设提示词不能为空，未改动角色包', false);
  try {
    await requestJson(`/api/characters/${profileId}/system-prompt`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ system_prompt: text }),
    });
    prof.system_prompt = text;
    // Rescanning may have replaced the profile objects while this request was pending.
    const currentProfile = voiceProfiles.find(p => p.id === profileId);
    if (currentProfile) currentProfile.system_prompt = text;
    // Compare later edits with what was actually saved, including reverting to the old text.
    if (editor === editorRevision && _promptOwnerId === profileId) {
      _promptBaseline = text;
    }
    return result(`；已写回角色包「${prof.name}」`);
  } catch (e) {
    return result(`；人设写入角色包失败：${e.message || e}`, false);
  }
}

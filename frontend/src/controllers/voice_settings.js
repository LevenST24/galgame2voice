// Voice Settings & Session Profile Controller
import { state, saveState, storageChangedByAnotherPage, getActive, DEFAULT_SESSION_SETTINGS } from '../store.js';
import { showToast } from '../ui.js';
import { requestJson } from '../api.js';
import { getErrorMessage } from '../request.js';
import { persistSession } from '../session_api.js';
import { getConsoleToken, promptConsoleToken } from '../auth.js';
import { packTarBlob } from '../tar_pack.js';

export const FIRST_USE_VOICE_HINT =
  '首次使用：点「导入角色包」选择角色包（文件夹或 .zip），也可以直接拖进来；没有角色包时，可以用模型权重和参考音频新建音色。';

let _dom = {};
let _callbacks = {};
let voiceProfiles = [];
// 面板里人设框的初始值与对应角色，用于换角色时判断"有未保存的编辑"
let _promptBaseline = '';
let _promptOwnerId = null;
let _promptSelection = '';
let editorRevision = 0;
let activeProfileId = null;
let lastSwitchFailed = false;
let failedSwitchProfileId = null;
let switchSaveUnconfirmed = false;
let scannedModels = null;
let pendingScan = null;
let scanRevision = 0;
let sessionVoiceRevision = 0;
let voiceSwitchTail = Promise.resolve();
let activeProfileRevision = 0;
let voiceSelectRevision = 0;

// 服务端响应里带中文说明时直接显示它（规则同 getErrorMessage）；否则返回空串，由调用处用默认文案。
const serverMessage = e => (e?.data ? getErrorMessage(e.data, '') : '');
// Value of the only option left when the voice list fails to load. It is not a choice:
// saving keeps the session's binding instead of treating it as "no character".
const VOICE_LIST_UNAVAILABLE = '__unavailable__';
let profilesRevision = 0;
let lastShownCatalogWarning = '';
let isImporting = false;
let isDeletingVoice = false;
let redeemCode = '';
let dragEnterDepth = 0;
let cleanupDragListeners = null;

export function invalidateScannedModels() {
  scannedModels = null;
  pendingScan = null;
  scanRevision += 1;
}

export function initVoiceSettings(dom, callbacks = {}) {
  cleanupDragListeners?.();
  cleanupDragListeners = null;
  isImporting = false;
  isDeletingVoice = false;
  redeemCode = '';
  dragEnterDepth = 0;

  _dom = dom;
  _callbacks = callbacks;
  syncVoiceDeleteButton();
  _promptBaseline = '';
  _promptOwnerId = null;
  _promptSelection = '';
  lastSwitchFailed = false;
  lastShownCatalogWarning = '';
  failedSwitchProfileId = null;
  switchSaveUnconfirmed = false;
  editorRevision += 1;
  sessionVoiceRevision += 1;
  profilesRevision += 1;
  activeProfileRevision += 1;

  _dom.sVoice?.addEventListener('change', handleVoiceSelectionChange);
  _dom.sVoiceImport?.addEventListener('click', () => !isImporting && _dom.sVoiceImportZipInput?.click());
  _dom.sVoiceImportFolder?.addEventListener('click', () => !isImporting && _dom.sVoiceImportDirInput?.click());
  _dom.sVoiceRedeem?.addEventListener('click', () => _dom.sVoiceRedeemPanel?.classList.toggle('hidden'));
  _dom.sVoiceRedeemCode?.addEventListener('keydown', handleRedeemCodeKeydown);
  _dom.sVoiceRedeemQuery?.addEventListener('click', queryRedeemCode);
  _dom.sVoiceImportZipInput?.addEventListener('change', handleZipInputChange);
  _dom.sVoiceImportDirInput?.addEventListener('change', handleFolderInputChange);
  _dom.sVoiceRescan?.addEventListener('click', handleRescanCharacters);
  _dom.sVoiceDelete?.addEventListener('click', deleteVoiceProfile);
  _dom.sCvCreate?.addEventListener('click', createCustomVoice);
  cleanupDragListeners = bindSessionDropTarget();

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

export function cancelSessionSettingsLoad() {
  editorRevision += 1;
  dragEnterDepth = 0;
  _dom.sVoiceDropOverlay?.classList?.add('hidden');
}

function syncInitialSessionVoiceSelection(session) {
  if (!_dom.sVoice) return;
  const initialVoiceId = session.settings?.voiceProfileId ? String(session.settings.voiceProfileId) : '';
  const options = Array.from(_dom.sVoice.options || _dom.sVoice.children || []);
  if (initialVoiceId && !options.some((opt) => String(opt.value) === initialVoiceId)) {
    // Until the profile list loads, keep the bound voice selected so a save cannot unbind it.
    const opt = document.createElement('option');
    opt.value = initialVoiceId;
    opt.textContent = voiceProfiles.find((p) => String(p.id) === initialVoiceId)?.name || `#${initialVoiceId}`;
    _dom.sVoice.appendChild(opt);
  }
  _dom.sVoice.value = initialVoiceId;
  syncCustomVoiceBox();
}

export async function openSessionSettings() {
  const s = getActive();
  if (!s) return;
  const revision = ++editorRevision;
  if (_dom.smTitle) _dom.smTitle.textContent = `· ${s.title}`;
  syncInitialSessionVoiceSelection(s);
  applyProfilePromptToField(s.settings?.voiceProfileId ?? null);
  if (_dom.sSystem) {
    // 人设提示词是角色级数据，权威源在角色包；这里只展示当前绑定角色的那一份
    if (!voiceProfiles.length) {
      try { await fetchVoiceProfiles(); } catch (_) { /* 下面按空值处理 */ }
    }
    if (getActive() !== s || revision !== editorRevision) return;
    const cleanPrompt = (_dom.sSystem.value || '').trim() === _promptBaseline.trim();
    if (cleanPrompt) {
      applyProfilePromptToField(s.settings?.voiceProfileId ?? null);
    }
  }
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
  syncInitialSessionVoiceSelection(s);
  if (_callbacks.openModal) _callbacks.openModal(_dom.sessionModal);
  // Opening the shared center starts a background catalog refresh. Start the
  // editor's own read afterwards so its render uses the newest catalog owner.
  loadSessionVoiceSelect(s);
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

function updateVoiceActiveHint(session) {
  const active = activeProfileId != null ? voiceProfiles.find((p) => Number(p.id) === Number(activeProfileId)) : null;
  if (_dom.sVoiceActive) {
    _dom.sVoiceActive.textContent = active
      ? `当前加载：${active.name}`
      : (!voiceProfiles.length && !session?.settings?.voiceProfileId ? FIRST_USE_VOICE_HINT : '');
  }
  if (_dom.sVoiceImport) {
    _dom.sVoiceImport.classList.toggle('primary', !voiceProfiles.length);
    _dom.sVoiceImport.classList.toggle('ghost', !!voiceProfiles.length);
  }
  return active;
}

function refreshActiveLoadedBadges() {
  updateVoiceActiveHint(getActive());
  const options = Array.from(_dom.sVoice?.options || _dom.sVoice?.children || []);
  for (const opt of options) {
    const optId = Number(opt.value);
    if (!optId) continue;
    const p = voiceProfiles.find((x) => Number(x.id) === optId);
    if (p) {
      opt.textContent = `${p.name}${activeProfileId != null && Number(p.id) === Number(activeProfileId) ? '（当前加载）' : ''}`;
    }
  }
  _callbacks.renderHeader?.();
}

export async function fetchVoiceProfiles({ silent = false } = {}) {
  const revision = ++profilesRevision;
  const activeRevision = activeProfileRevision;
  const data = await requestJson('/api/voice/profiles');
  if (revision === profilesRevision) {
    voiceProfiles = data.profiles || [];
    if (!data.catalog_warning) {
      lastShownCatalogWarning = '';
    } else if (!silent || data.catalog_warning !== lastShownCatalogWarning) {
      showToast(data.catalog_warning, 'error');
      lastShownCatalogWarning = data.catalog_warning;
    }
    // A catalog response may predate a completed switch. Keep its catalog,
    // but never publish that earlier active ID over the switch acknowledgement.
    if (activeRevision === activeProfileRevision) {
      const serverActiveId = data.active_profile_id;
      const failedTargetRemoved = failedSwitchProfileId != null
        && !voiceProfiles.some(p => Number(p.id) === Number(failedSwitchProfileId));
      const serverConfirmedOtherValid = serverActiveId != null
        && (failedSwitchProfileId == null || Number(serverActiveId) !== Number(failedSwitchProfileId))
        && voiceProfiles.some(p => Number(p.id) === Number(serverActiveId))
        && !switchSaveUnconfirmed;

      if (!lastSwitchFailed || failedTargetRemoved || serverConfirmedOtherValid) {
        const recovered = lastSwitchFailed && (failedTargetRemoved || serverConfirmedOtherValid);
        if (recovered) {
          lastSwitchFailed = false;
          failedSwitchProfileId = null;
          switchSaveUnconfirmed = false;
        }
        activeProfileId = serverActiveId;
        if (recovered) {
          refreshActiveLoadedBadges();
        }
      }
    }
  }
  return data;
}

export function ensureSessionVoice(session, options = {}) {
  const revision = ++sessionVoiceRevision;
  const want = session?.settings?.voiceProfileId;
  const isCurrent = () => revision === sessionVoiceRevision
    && (!session?.id || getActive()?.id === session.id)
    && session?.settings?.voiceProfileId === want;
  // Ignoring an old response only protects the UI; its server write still
  // happens. Send switches in order, skipping choices superseded while queued.
  const task = voiceSwitchTail.then(() => {
    if (isCurrent()) return applySessionVoice(session, options, want, isCurrent);
  });
  voiceSwitchTail = task.catch(() => {});
  return task;
}

async function applySessionVoice(session, { silent = false }, want, isCurrent) {
  if (activeProfileId === null || !want) {
    try {
      await fetchVoiceProfiles({ silent });
      if (!isCurrent()) return;
      _callbacks.renderHeader?.();
    } catch { /* Text and ADV art remain available without the voice service. */ }
  }
  if (!want) return;
  try {
    if (!isCurrent()) return;
    activeProfileRevision += 1;
    let data;
    try {
      data = await requestJson('/api/voice/switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile_id: want }),
      }, { timeoutMs: 120000 });
    } finally {
      activeProfileRevision += 1;
    }
    if (!isCurrent()) return;
    lastSwitchFailed = false;
    failedSwitchProfileId = null;
    switchSaveUnconfirmed = false;
    activeProfileId = want;
    // 换人后若面板正开着且人设框没有未保存编辑，就跟着换成新角色的那一份
    const modalOpen = _dom.sessionModal && !_dom.sessionModal.classList.contains('hidden');
    if (modalOpen && _dom.sSystem) {
      const clean = (_dom.sSystem.value || '').trim() === _promptBaseline.trim();
      if (clean && String(_dom.sVoice?.value) === String(want)) applyProfilePromptToField(want);
    }
    if (_callbacks.renderHeader) _callbacks.renderHeader();
    if (!silent && !_callbacks.isHomeOpen?.()) showToast(`已切换音色：${data.profile || want}`, 'success');
  } catch (e) {
    if (!isCurrent()) return;
    lastSwitchFailed = true;
    failedSwitchProfileId = want != null ? Number(want) : null;
    activeProfileId = null;
    switchSaveUnconfirmed = e.data?.detail?.error_code === 'VOICE_PROFILE_SAVE_FAILED';
    if (switchSaveUnconfirmed) {
      // The models may be loaded even when the saved default is unconfirmed.
      // Older list reads cannot turn that uncertainty back into a known ID.
      activeProfileRevision += 1;
      activeProfileId = null;
      _callbacks.renderHeader?.();
      showToast(e.message, 'error', _callbacks.onOpenSessionSettings ? {
        actionLabel: '重新选择音色',
        onAction: () => _callbacks.onOpenSessionSettings(),
      } : null);
      return;
    }
    if (e.status === 404) {
      try {
        await fetchVoiceProfiles();
        if (!isCurrent()) return;
        if (!voiceProfiles.some(profile => profile.id === want)
          && session?.settings?.voiceProfileId === want) {
          _callbacks.renderHeader?.();
          const msg = '这个会话绑定的音色已被删除，原选择已保留。请在「角色与会话」选择新音色，或明确选择“不指定（沿用语音服务当前的声音）”。';
          showToast(msg, 'info', _callbacks.onOpenSessionSettings ? {
            actionLabel: '选择音色',
            onAction: () => _callbacks.onOpenSessionSettings(),
          } : null);
          return;
        }
      } catch { /* Keep the binding when its existence cannot be checked. */ }
    }
    if (_callbacks.isHomeOpen?.()) return;
    console.error('音色切换失败:', e);
    showToast('语音引擎还没准备好，现在只有文字、没有声音。可在「运行状态」查看引擎。', 'error', _callbacks.onOpenEngineStatus ? {
      actionLabel: '检查运行状态',
      onAction: () => _callbacks.onOpenEngineStatus(),
    } : null);
  }
}

export async function loadSessionVoiceSelect(session, { preserveDraft = false, syncCustomBox = true } = {}) {
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
    _dom.sVoice.innerHTML = '<option value="">不指定（沿用语音服务当前的声音）</option>';
    const active = updateVoiceActiveHint(session);
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
    const selection = keepDraft ? draftSelection : String(session?.settings?.voiceProfileId || '');
    if (selection && selection !== '__custom__'
        && !voiceProfiles.some(p => String(p.id) === selection)) {
      const missingOpt = document.createElement('option');
      missingOpt.value = selection;
      missingOpt.textContent = keepDraft ? '原音色已不可用（人设草稿已保留，请重新选择音色）' : '原音色已不可用，请重新选择音色';
      missingOpt.disabled = true;
      _dom.sVoice.appendChild(missingOpt);
    }
    if (keepDraft) {
      _dom.sVoice.value = draftSelection;
    } else if (session?.settings?.voiceProfileId) {
      _dom.sVoice.value = String(session.settings.voiceProfileId);
    } else if (!voiceProfiles.length) {
      _dom.sVoice.value = '__custom__';
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
      _dom.sVoice.innerHTML = `<option value="${VOICE_LIST_UNAVAILABLE}">加载失败，请先启动程序后重新打开此面板</option>`;
      _dom.sVoice.value = VOICE_LIST_UNAVAILABLE;
    } else {
      // Let the rescan report a failed refresh rather than claiming success.
      throw e;
    }
  }
  if (syncCustomBox) {
    syncCustomVoiceBox();
  }
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

function syncVoiceDeleteButton() {
  const id = Number(_dom.sVoice?.value);
  if (_dom.sVoiceDelete) _dom.sVoiceDelete.disabled = isDeletingVoice || !id || !voiceProfiles.some((p) => p.id === id);
}

export function syncCustomVoiceBox() {
  syncVoiceDeleteButton();
  if (!_dom.sVoice || !_dom.sCustomVoiceBox) return;
  const show = _dom.sVoice.value === '__custom__';
  _dom.sCustomVoiceBox.classList.toggle('hidden', !show);
  if (show) populateScanOptions();
}

function handleVoiceSelectionChange() {
  syncSystemPromptToSelectedVoice();
  syncCustomVoiceBox();
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
    fill(dom.sCvGpt, scannedModels.gpt_weights || [], '（未扫描到 .ckpt 文件；请先在「设置中心 → 语音服务」选择引擎目录）');
    fill(dom.sCvSovits, scannedModels.sovits_weights || [], '（未扫描到 .pth 文件；请先在「设置中心 → 语音服务」选择引擎目录）');
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
    showToast('必须选择参考音频：没有参考音频，合成出的音色会严重失真。', 'error');
    return;
  }
  if (!promptText) {
    showToast('必须填写参考音频里说的话，留空将无法合成语音。', 'error');
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
      const version = (origin.unconfirmedSettingsVersion || 0) + 1;
      origin.unconfirmedSettingsVersion = version;
      saveState();
      if (storageChangedByAnotherPage()) {
        note = '；当前网页已暂停保存，会话绑定未提交到本机服务';
      } else {
        try {
          await persistSession(origin);
          if (origin.unconfirmedSettingsVersion === version) {
            delete origin.unconfirmedSettingsVersion;
            saveState();
          }
        }
        catch (e) { note = `；会话绑定尚未同步到本机服务：${e.message}`; }
      }
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
    const binding = state.sessions.includes(origin) ? `，已绑定到「${origin.title}」` : '，可在“角色与会话”中选择';
    showToast(`音色「${data.name}」已创建${binding}${note}`, note ? 'info' : 'success');
  } catch (e) {
    console.error('音色创建失败:', e);
    showToast(serverMessage(e) || '音色创建失败：当前输入已保留，请检查参考音频与模型文件后重试', 'error');
  } finally {
    _dom.sCvCreate.disabled = false;
  }
}

export async function handleRescanCharacters() {
  if (isImporting || _dom.sVoiceRescan?.disabled) return;
  if (_dom.sVoiceRescan) _dom.sVoiceRescan.disabled = true;
  try {
    const res = await requestJson('/api/characters/rescan', { method: 'POST' }, { timeoutMs: 30000 });
    invalidateScannedModels();
    await loadSessionVoiceSelect(getActive(), { preserveDraft: true });
    if (_dom.sVoice?.value === '__custom__') {
      await populateScanOptions();
    }
    // The server names each rejected folder and why; the voice list shows the same notice.
    if (res?.catalog_warning) {
      showToast(res.catalog_warning, 'error');
    } else {
      const count = res?.discovered_count ?? 0;
      showToast(count > 0 ? `已扫描并同步 ${count} 个角色包` : '已完成扫描（未发现新的角色包文件夹）', 'success');
    }
  } catch (e) {
    console.error('扫描角色包失败:', e);
    showToast('扫描角色包失败：现有角色包列表保持不变，请检查角色包目录后重试', 'error');
  } finally {
    if (_dom.sVoiceRescan && !isImporting) _dom.sVoiceRescan.disabled = false;
  }
}

function setImportBusy(busy) {
  isImporting = busy;
  if (_dom.sVoiceImport) {
    _dom.sVoiceImport.disabled = busy;
    _dom.sVoiceImport.textContent = busy ? '正在导入…' : '导入角色包';
  }
  if (_dom.sVoiceImportFolder) _dom.sVoiceImportFolder.disabled = busy;
  if (_dom.sVoiceRedeem) _dom.sVoiceRedeem.disabled = busy;
  if (_dom.sVoiceRedeemQuery) _dom.sVoiceRedeemQuery.disabled = busy;
  if (_dom.sVoiceRescan) _dom.sVoiceRescan.disabled = busy;
}

function sendImportArchiveXhr(blob, filename, { overwrite = false, retriedAuth = false } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const qs = new URLSearchParams([...(filename ? [['filename', filename]] : []), ...(overwrite ? [['overwrite', '1']] : [])]).toString();
    xhr.open('POST', `/api/characters/import${qs ? `?${qs}` : ''}`);
    xhr.setRequestHeader('Content-Type', blob.type || 'application/octet-stream');
    const token = getConsoleToken();
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
    if (xhr.upload) xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable && ev.total > 0 && _dom.sVoiceImport) {
        _dom.sVoiceImport.textContent = `正在导入…${Math.min(100, Math.max(0, Math.round((ev.loaded / ev.total) * 100)))}%`;
      }
    };
    xhr.onload = async () => {
      if (xhr.status === 401 && !retriedAuth) {
        let token = null;
        try { token = await promptConsoleToken('服务需要访问令牌验证'); } catch {}
        if (token) {
          sendImportArchiveXhr(blob, filename, { overwrite, retriedAuth: true }).then(resolve, reject);
          return;
        }
      }
      let data = null;
      try { data = xhr.responseText ? JSON.parse(xhr.responseText) : null; } catch {}
      resolve({ status: xhr.status, data });
    };
    xhr.onerror = () => reject(new Error('无法连接本机服务，请确认程序正在运行后重试。'));
    xhr.onabort = () => reject(new Error('导入已取消。'));
    xhr.send(blob);
  });
}

function confirmPackageOverwrite(data, filename) {
  const names = Array.isArray(data?.names) && data.names.length ? data.names.join('、') : (filename || '该角色');
  return window.confirm(`已有同名角色包「${names}」，覆盖它吗？旧的会先备份。`);
}

async function finishCharacterImport(data, filename) {
  invalidateScannedModels();
  await loadSessionVoiceSelect(getActive(), { preserveDraft: true, syncCustomBox: false });
  const imported = Array.isArray(data?.imported) ? data.imported : [];
  if (imported[0]?.profile_id != null && _dom.sVoice) {
    _dom.sVoice.value = String(imported[0].profile_id);
    handleVoiceSelectionChange();
  } else syncCustomVoiceBox();
  const label = imported.length ? imported.map((item) => `「${item.name}」`).join('、') : `「${filename || '角色包'}」`;
  showToast(`已导入${label}，点「保存」即可使用`, 'success');
}

async function importCharacterArchive(blob, filename) {
  if (isImporting || !blob) return;
  setImportBusy(true);
  try {
    let { status, data } = await sendImportArchiveXhr(blob, filename, { overwrite: false });
    if (status === 409 && data?.code === 'package_exists') {
      if (!confirmPackageOverwrite(data, filename)) return;
      if (_dom.sVoiceImport) _dom.sVoiceImport.textContent = '正在导入…';
      ({ status, data } = await sendImportArchiveXhr(blob, filename, { overwrite: true }));
    }
    if (status < 200 || status >= 300) {
      showToast(getErrorMessage(data, '') || `导入角色包失败（HTTP ${status}）`, 'error');
      return;
    }
    await finishCharacterImport(data, filename);
  } catch (err) {
    console.error('导入角色包失败:', err);
    showToast(err?.message || '导入角色包失败，请重试。', 'error');
  } finally {
    setImportBusy(false);
  }
}

const _KB = 1024, _MB = 1024 ** 2, _GB = 1024 ** 3;
function redeemUnit(n) {
  if (n >= _GB) return [_GB, 'GB'];
  if (n >= _MB) return [_MB, 'MB'];
  if (n >= _KB) return [_KB, 'KB'];
  return [1, 'B'];
}
function redeemScaled(n, size) {
  return size === 1 ? String(Math.round(n)) : (n / size).toFixed(1);
}
export function formatRedeemProgress(progress) {
  if (progress.phase === 'import') return '正在导入…';
  if (progress.phase !== 'download') return '';
  const done = Number(progress.done) || 0, total = Number(progress.total) || 0;
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const [size, label] = redeemUnit(Math.max(done, total));
  return `正在下载「${progress.name || ''}」… ${pct}%（${redeemScaled(done, size)} / ${redeemScaled(total, size)} ${label}）`;
}
function formatRedeemSize(n) {
  const value = Number(n) || 0, [size, label] = redeemUnit(value);
  return `${redeemScaled(value, size)} ${label}`;
}

function renderRedeemList(packages) {
  const list = _dom.sVoiceRedeemList;
  if (!list) return;
  list.innerHTML = '';
  for (const pkg of packages) {
    const row = document.createElement('div');
    row.className = 'redeem-item';
    const label = document.createElement('span');
    label.textContent = `${pkg.name} · ${formatRedeemSize(pkg.size)} · 还可下载 ${pkg.remaining} 次`;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn ghost';
    button.textContent = '下载并导入';
    button.addEventListener('click', () => downloadRedeemPackage(pkg));
    row.appendChild(label);
    row.appendChild(button);
    list.appendChild(row);
  }
}

function handleRedeemCodeKeydown(event) {
  if (event.key !== 'Enter' || event.isComposing || event.keyCode === 229) {
    return;
  }
  event.preventDefault();
  return queryRedeemCode();
}

async function queryRedeemCode() {
  const input = _dom.sVoiceRedeemCode;
  const code = String(input?.value || '').trim();
  if (!code) return showToast('请先粘贴兑换码', 'error');
  if (isImporting) return;
  try {
    const data = await requestJson('/api/characters/redeem/list', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    }, { timeoutMs: 45000 });
    redeemCode = code;
    const packages = Array.isArray(data?.packages) ? data.packages : [];
    renderRedeemList(packages);
    if (input) input.value = '';
    if (_dom.sVoiceRedeemStatus) {
      _dom.sVoiceRedeemStatus.textContent = packages.length ? '' : '这个兑换码下没有可下载的角色包。';
    }
  } catch (err) {
    redeemCode = '';
    renderRedeemList([]);
    showToast(err?.message || '查询兑换码失败', 'error');
  }
}

async function pollRedeemStatus() {
  if (!isImporting || !_dom.sVoiceRedeemStatus) return;
  try {
    const progress = await requestJson('/api/characters/redeem/progress', {}, { timeoutMs: 10000 });
    if (!isImporting || !_dom.sVoiceRedeemStatus) return;
    _dom.sVoiceRedeemStatus.textContent = formatRedeemProgress(progress);
  } catch {
    // 下一秒再查
  }
}

async function downloadRedeemPackage(pkg) {
  if (isImporting || !redeemCode || !pkg.id) return;
  setImportBusy(true);
  if (_dom.sVoiceRedeemStatus) {
    _dom.sVoiceRedeemStatus.textContent = formatRedeemProgress({
      phase: 'download',
      done: 0,
      total: Number(pkg.size) || 0,
      name: pkg.name,
    });
  }
  const timer = setInterval(pollRedeemStatus, 1000);
  const post = (overwrite) => requestJson('/api/characters/redeem/download', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code: redeemCode, package_id: pkg.id, overwrite }),
  }, { timeoutMs: 0 });
  try {
    let data;
    try {
      data = await post(false);
    } catch (err) {
      const conflict = err?.status === 409 && err?.data?.code === 'package_exists';
      if (!conflict || !confirmPackageOverwrite(err.data, pkg.name)) throw err;
      if (_dom.sVoiceRedeemStatus) _dom.sVoiceRedeemStatus.textContent = '正在导入…';
      data = await post(true);
    }
    await finishCharacterImport(data, pkg.name);
  } catch (err) {
    if (err?.status === 409 && err?.data?.code === 'package_exists') return;
    showToast(err?.message || '下载并导入失败，请重试。', 'error');
  } finally {
    clearInterval(timer);
    setImportBusy(false);
    if (_dom.sVoiceRedeemStatus) _dom.sVoiceRedeemStatus.textContent = '';
  }
}

async function handleZipInputChange() {
  const input = _dom.sVoiceImportZipInput, file = input?.files?.[0];
  if (input) input.value = '';
  if (file) await importCharacterArchive(file, file.name || 'character_package.zip');
}

async function handleFolderInputChange() {
  const input = _dom.sVoiceImportDirInput, files = Array.from(input?.files || []);
  if (input) input.value = '';
  if (!files.length) return;
  try {
    const entries = files.map((f) => ({ path: String(f.webkitRelativePath || f.name || '').trim(), file: f })).filter((e) => e.path);
    await importCharacterArchive(packTarBlob(entries), entries[0]?.path?.split('/')[0] || 'character_package');
  } catch (err) { showToast(err?.message || '打包角色包文件夹失败。', 'error'); }
}

async function collectEntryFiles(entry, parentPath = '') {
  const relPath = parentPath ? `${parentPath}/${entry.name}` : entry.name;
  if (entry.isFile) return [{ path: relPath, file: await new Promise((res, rej) => entry.file(res, rej)) }];
  if (!entry.isDirectory || entry.name === '__MACOSX') return [];
  const reader = entry.createReader();
  const children = await new Promise((res, rej) => {
    const all = [];
    const next = () => reader.readEntries((b) => (!b?.length ? res(all) : (all.push(...b), next())), rej);
    next();
  });
  const out = [];
  for (const child of children) out.push(...(await collectEntryFiles(child, relPath)));
  return out;
}

function bindSessionDropTarget() {
  const target = _dom.sessionModal;
  if (!target) return null;
  const active = () => !target.hidden && !target.classList?.contains('hidden');
  const hasFiles = (ev) => Array.from(ev?.dataTransfer?.types || []).includes('Files');
  const setOverlay = (on) => _dom.sVoiceDropOverlay?.classList?.toggle('hidden', !on);
  const handlers = {
    dragenter: (ev) => {
      if (isImporting || !active() || !hasFiles(ev)) return;
      ev.preventDefault?.(); dragEnterDepth += 1; setOverlay(true);
    },
    dragover: (ev) => {
      if (isImporting || !active() || !hasFiles(ev)) return;
      ev.preventDefault?.(); setOverlay(true);
      if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'copy';
    },
    dragleave: () => {
      if (active() && (dragEnterDepth = Math.max(0, dragEnterDepth - 1)) === 0) setOverlay(false);
    },
    drop: async (ev) => {
      if (!active() || !hasFiles(ev)) return;
      ev.preventDefault?.(); ev.stopPropagation?.();
      dragEnterDepth = 0; setOverlay(false);
      if (isImporting) return;
      const entries = Array.from(ev.dataTransfer?.items || []).map((it) => it?.webkitGetAsEntry?.()).filter(Boolean);
      if (!entries.length) return;
      try {
        if (entries.length === 1 && entries[0].isFile) {
          const file = await new Promise((res, rej) => entries[0].file(res, rej));
          return await importCharacterArchive(file, file.name || entries[0].name || 'character_package');
        }
        if (entries.some((e) => e.isFile && /\.(zip|tar|7z|rar)$/i.test(e.name || ''))) {
          return showToast('请一次拖入一个压缩包，或拖入包含多个角色包的文件夹。', 'error');
        }
        const collected = [];
        for (const entry of entries) collected.push(...(await collectEntryFiles(entry)));
        if (!collected.length) return showToast('拖入的文件夹为空，请选择包含 manifest.json 的角色包。', 'error');
        // Several loose items (e.g. the files inside a package folder) have no folder name: the server takes the manifest's.
        await importCharacterArchive(packTarBlob(collected), entries.length === 1 ? entries[0].name : '');
      } catch (err) {
        console.error('拖放导入角色包失败:', err);
        showToast(err?.message || '读取拖入的角色包失败，请改用「导入角色包」按钮选择。', 'error');
      }
    },
  };
  for (const [type, fn] of Object.entries(handlers)) target.addEventListener?.(type, fn);
  return () => { for (const [type, fn] of Object.entries(handlers)) target.removeEventListener?.(type, fn); };
}

export async function deleteVoiceProfile() {
  if (_dom.sVoiceDelete.disabled) return;
  const id = Number(_dom.sVoice.value);
  if (!id) {
    showToast('请先在下拉框中选择要删除的音色', 'error');
    return;
  }
  const activeSession = getActive();
  if (
    id === activeProfileId
    && !lastSwitchFailed
    && voiceProfiles.length > 1
    && activeSession?.settings?.voiceProfileId === id
  ) {
    showToast('该音色正在被引擎加载，请先把会话切换到其他音色再删除', 'error');
    return;
  }
  const p = voiceProfiles.find((x) => x.id === id);
  if (!window.confirm(`确定删除音色「${p ? p.name : id}」？此操作不可恢复。`)) return;
  isDeletingVoice = true;
  syncVoiceDeleteButton();
  try {
    await requestJson(`/api/voice/profiles/${id}`, { method: 'DELETE' });
    if (id === activeProfileId) activeProfileId = null;
    if (failedSwitchProfileId != null && Number(id) === Number(failedSwitchProfileId)) {
      lastSwitchFailed = false;
      failedSwitchProfileId = null;
      switchSaveUnconfirmed = false;
    }
    const cleared = state.sessions.filter(sess => sess.settings?.voiceProfileId === id);
    const sessionVersions = new Map();
    for (const sess of cleared) {
      sess.settings.voiceProfileId = null;
      const version = (sess.unconfirmedSettingsVersion || 0) + 1;
      sess.unconfirmedSettingsVersion = version;
      sessionVersions.set(sess, version);
    }
    let bindingNote = '';
    if (cleared.length) {
      _callbacks.invalidateSessionSync?.();
      saveState();
      if (storageChangedByAnotherPage()) {
        bindingNote = '；当前网页已暂停保存，解除绑定未提交到本机服务';
      } else {
        // The server copies still name the deleted voice; the next sync would restore them.
        for (const sess of cleared) {
          const version = sessionVersions.get(sess);
          persistSession(sess).then(() => {
            if (sess.unconfirmedSettingsVersion === version) {
              delete sess.unconfirmedSettingsVersion;
              saveState();
            }
          }).catch(() => {});
        }
        bindingNote = `，并解除了 ${cleared.length} 个会话的绑定`;
      }
    }
    await loadSessionVoiceSelect(getActive());
    refreshActiveLoadedBadges();
    ensureSessionVoice(getActive(), { silent: true });
    showToast(`音色「${p ? p.name : id}」已删除${bindingNote}`, bindingNote.startsWith('；') ? 'info' : 'success');
  } catch (e) {
    console.error('音色删除失败:', e);
    showToast(serverMessage(e) || '音色删除失败：该音色已被保留，请稍后重试；若仍失败，请打开「运行状态」检查服务', 'error');
  } finally {
    isDeletingVoice = false;
    syncVoiceDeleteButton();
  }
}

const EDITOR_FIELDS = [
  'sVoice', 'sSystem', 'sTemp', 'sTopP', 'sMaxTokens', 'sFreq', 'sPres', 'sCtx',
  'sAiAdaptiveVoice', 'sTtsSpeed', 'sTtsTopK', 'sTtsTopP', 'sTtsTemp',
  'sCvName', 'sCvGpt', 'sCvSovits', 'sCvRef', 'sCvPromptText', 'sCvLang',
];
const editorSnapshot = () => JSON.stringify(EDITOR_FIELDS.map(key => _dom[key]?.value));

async function handleSaveSessionSettings() {
  if (_dom.sSave?.disabled) return;
  const s = getActive();
  if (!s) return;
  const editor = editorRevision;
  const submitted = editorSnapshot();
  const sameEditor = () => getActive() === s && editor === editorRevision;
  if (_dom.sSave) _dom.sSave.disabled = true;
  try {
    if (_dom.sVoice.value === '__custom__') {
      const isFirstUseUntouchedCustom = !voiceProfiles.length
        && !s.settings?.voiceProfileId
        && !(_dom.sCvName?.value || '').trim()
        && !(_dom.sCvPromptText?.value || '').trim()
        && !(_dom.sSystem?.value || '').trim();
      if (!isFirstUseUntouchedCustom) {
        showToast('请先点击「创建并绑定」完成新音色的创建，再保存会话参数。填写的内容已保留。', 'info');
        return;
      }
    }
    _callbacks.invalidateSessionSync?.();
    const maxTokens = Math.round(Number(_dom.sMaxTokens.value)) || DEFAULT_SESSION_SETTINGS.maxTokens;
    const voiceListUnavailable = _dom.sVoice.value === VOICE_LIST_UNAVAILABLE;
    let profileId = _dom.sVoice.value && _dom.sVoice.value !== '__custom__'
      ? Number(_dom.sVoice.value)
      : null;
    if (voiceListUnavailable) profileId = s.settings?.voiceProfileId ?? null;
    const previousProfileId = s.settings?.voiceProfileId ?? null;
    const missingBinding = !voiceListUnavailable && profileId === s.settings?.voiceProfileId
      && profileId && !voiceProfiles.some(p => p.id === profileId)
      && _promptOwnerId === profileId && (_dom.sSystem?.value || '').trim() === _promptBaseline.trim();
    if (!voiceListUnavailable && profileId
        && !missingBinding && (_promptOwnerId !== profileId || !voiceProfiles.some(p => p.id === profileId))) {
      showToast('人设草稿与当前角色不一致，已保留编辑内容。请重新选择对应角色后再保存。', 'error');
      return;
    }
    s.settings = {
      ...s.settings,
      voiceProfileId: profileId,
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
    const version = (s.unconfirmedSettingsVersion || 0) + 1;
    s.unconfirmedSettingsVersion = version;
    saveState();

    // 人设提示词属于角色、不属于会话：写回角色包 manifest.json，DB 镜像由后端刷新
    const { note: promptNote, confirmed: promptConfirmed } = missingBinding
      ? { note: '；原音色已不可用，音色绑定已保留，请重新选择音色', confirmed: true }
      : voiceListUnavailable
      ? { note: '；角色列表未能加载，角色绑定与人设保持不变', confirmed: false }
      : await saveCharacterPrompt(profileId, editor);

    let syncNote = '';
    if (storageChangedByAnotherPage()) {
      syncNote = '；当前网页已暂停保存，设置未提交到本机服务';
    } else {
      try {
        // The session may have been deleted while its character prompt was saving.
        if (state.sessions.includes(s)) {
          await persistSession(s);
          if (s.unconfirmedSettingsVersion === version) {
            delete s.unconfirmedSettingsVersion;
            saveState();
          }
        }
      } catch (err) {
        syncNote = `；本机服务尚未确认保存：${err.message}`;
      }
    }

    if (sameEditor() && editorSnapshot() === submitted && promptConfirmed && !syncNote && _callbacks.closeModal) {
      _callbacks.closeModal(_dom.sessionModal);
    }
    if (_callbacks.renderSidebar) _callbacks.renderSidebar();
    if (_callbacks.renderHeader) _callbacks.renderHeader();
          if (getActive() === s) await ensureSessionVoice(s);
    const draftNote = sameEditor() && editorSnapshot() !== submitted ? '；保存期间新增的修改已保留在面板中，请再次保存' : '';
    const nextName = voiceProfiles.find((p) => p.id === profileId)?.name;
    const lead = nextName && profileId !== previousProfileId ? `已换上「${nextName}」。` : '设置已保存。';
    showToast(`${lead}${promptNote}${syncNote}${draftNote}`, promptNote || syncNote || draftNote ? 'info' : 'success');
  } finally {
    if (_dom.sSave) _dom.sSave.disabled = false;
  }
}

/** 保存当前音色的人设；属于角色包时由接口写回 manifest。失败时保留面板中的编辑内容。 */
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
    const saved = await requestJson(`/api/characters/${profileId}/system-prompt`, {
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
    return result(saved?.manifest_path
      ? `；人设已写回角色包「${prof.name}」`
      : `；人设已保存至音色「${prof.name}」`);
  } catch (e) {
    return result(`；人设写入角色包失败：${e.message || e}`, false);
  }
}

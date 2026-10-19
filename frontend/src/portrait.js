/**
 * 角色立绘舞台演出控制器 (Galgame Character Portrait Stage System)
 * 使用后端统一的立绘数据，兼容 portrait/expressions.json 和旧版 manifest.json：
 * 交叉淡入淡出、逐句同情绪微漂移、好感度解锁红脸差分、服装差分切换、发声静态光晕。
 */

import { requestJson } from './request.js';

// 对话管线会请求的情绪 → 中文标签
export const EMOTION_LABELS = {
  gentle: '温柔', happy: '开心', shy: '害羞', tsundere: '傲娇',
  cool: '冷静', sad: '感伤', angry: '生气', surprised: '惊讶', thinking: '沉思',
};

export const KNOWN_EMOTION_KEYS = new Set(Object.keys(EMOTION_LABELS));

// 无情绪标签时的兜底推断（如后端未开启自适应音色则情绪为空）
const KEYWORD_EMOTIONS = [
  ['shy', /害羞|脸红|别看我|恥ずかしい|バカ/i],
  ['angry', /生气|火大|信じられない|何様/i],
  ['tsundere', /哼|才没有|真是的|別に|今さら/i],
  ['happy', /高兴|开心|太好了|谢谢|ふふ|ありがとう|嬉しい/i],
  ['sad', /寂寞|难过|孤独|寂しい/i],
  ['cool', /无聊|随便|敷衍|勝手に/i],
];

export class PortraitStageController {
  constructor() {
    this.appEl = null;
    this.stageEl = null;
    this.toggleBtn = null;
    this.collapseBtn = null;
    this.costumeToggleBtn = null;
    this.costumeIconEl = null;
    this.costumeBarEl = null;
    this.charNameEl = null;
    this.emotionLabelEl = null;
    this.viewportEl = null;
    this.imgA = null;
    this.imgB = null;
    this.dialogueCardEl = null;
    this.dialogueTextEl = null;
    this.dialogueNameEl = null;

    this.activeSlot = 'A';
    this.isVisible = true;
    this.mobileOpen = false;
    this.isSpeaking = false;
    this.preloaded = new Set();

    // 角色包立绘数据
    this.enabled = false;
    this.costumes = [];          // [{id,name,icon}]
    this.currentCostume = null;
    this.sprites = {};           // costumeId -> faceId -> {file,width,height}
    this.faces = {};             // faceId -> {label,primary,parts,blush,...}
    this.expressionSets = {};    // emotion -> [{id,strength}]
    this.currentEmotion = 'gentle';
    this.activeFace = null;
    this.recentFaces = [];
    this.affectionLevel = 1;
    this.currentCharacter = '';
    this.profileId = null;
    // Set once a character's portrait data has loaded, so a failed load is retried.
    this.loadedProfileId = null;
    this.lastPaint = null;
    this.emptyStateEl = null;
    this.characterRevision = 0;
    this.paintRevision = 0;
    this.characterRequest = null;
  }

  init({ profileId = null } = {}) {
    this.appEl = document.querySelector('.app');
    this.stageEl = document.getElementById('characterStage');
    this.toggleBtn = document.getElementById('portraitToggleBtn');
    this.collapseBtn = document.getElementById('stageCollapseBtn');
    this.costumeToggleBtn = document.getElementById('stageCostumeToggleBtn');
    this.costumeIconEl = document.getElementById('stageCostumeIcon');
    this.costumeBarEl = document.getElementById('stageCostumeBar');
    this.charNameEl = document.getElementById('stageCharacterName');
    this.emotionLabelEl = document.getElementById('stageEmotionLabel');
    this.viewportEl = document.getElementById('stageViewport');
    this.emptyStateEl = document.getElementById('stageEmptyState');
    this.imgA = document.getElementById('portraitImgA');
    this.imgB = document.getElementById('portraitImgB');
    this.dialogueCardEl = document.getElementById('stageDialogueCard');
    this.dialogueTextEl = document.getElementById('stageDialogueText');
    this.dialogueNameEl = document.getElementById('stageDialogueName');

    if (!this.stageEl || !this.appEl) return;

    let savedVisible = null;
    try { savedVisible = localStorage.getItem('g2v_portrait_visible'); } catch { /* browser storage may be disabled */ }
    if (savedVisible !== null) {
      this.isVisible = savedVisible === 'true';
    } else {
      this.isVisible = window.innerWidth >= 1200;
    }
    this.applyVisibility();

    window.addEventListener('resize', () => {
      if (!this.isMobile()) this.mobileOpen = false;
      this.applyVisibility();
    });

    this.bindEvents();
    if (profileId) this.setCharacter('', profileId);
    else this.initDefaultCharacter();
  }

  async initDefaultCharacter() {
    const revision = this.characterRevision;
    try {
      const data = await requestJson('/api/characters', {}, { timeoutMs: 15000 });
      if (revision !== this.characterRevision) return;
      const chars = data.characters || [];
      const active = chars.find((c) => c.is_active) || chars.find((c) => c.is_default) || chars[0];
      if (active && active.name) {
        await this.setCharacter(active.name, active.id);
      } else {
        await this.setCharacter('', null);
      }
    } catch (_) {
      if (revision === this.characterRevision) {
        this.applyPayload(null);
        this.setPortraitStatus('暂时无法加载角色列表，请检查本机服务后重新打开立绘模式。');
      }
    }
  }

  bindEvents() {
    if (this.toggleBtn) {
      this.toggleBtn.addEventListener('click', () => this.toggleVisibility());
    }
    if (this.collapseBtn) {
      this.collapseBtn.addEventListener('click', () => this.setVisibility(false));
    }
    if (this.costumeToggleBtn) {
      this.costumeToggleBtn.addEventListener('click', () => {
        if (this.costumes.length < 2) return;
        const ids = this.costumes.map((c) => c.id);
        const next = ids[(ids.indexOf(this.currentCostume) + 1) % ids.length];
        this.setCostume(next);
      });
    }
    document.getElementById('stageBackdrop')?.addEventListener('click', () => this.dismissMobile());
  }

  async loadPortrait(profileId, signal) {
    if (!profileId) return null;
    const data = await requestJson(`/api/characters/${encodeURIComponent(profileId)}/portrait`,
      { signal }, { timeoutMs: 15000 });
    return data && data.enabled ? data : null;
  }

  applyPayload(data) {
    this.paintRevision += 1;
    this.enabled = Boolean(data);
    if (!data) {
      this.costumes = [];
      this.sprites = {};
      this.faces = {};
      this.expressionSets = {};
      this.currentCostume = null;
      this.activeFace = null;
      this.recentFaces = [];
      this.lastPaint = null;
      this.renderCostumeBar();
      // 该角色包没有立绘差分：收掉两张图，舞台只留名字与台词卡
      for (const img of [this.imgA, this.imgB]) {
        if (img) {
          img.onload = null;
          img.onerror = null;
          img.removeAttribute('src');
          img.classList.remove('is-visible');
          img.classList.add('is-hidden');
        }
      }
      return;
    }
    this.costumes = (data.costumes || []).filter((c) => c && c.id);
    this.sprites = data.sprites || {};
    this.faces = data.faces || {};
    this.expressionSets = data.expression_sets || {};
    this.currentCostume = this.costumes.some((c) => c.id === data.default_costume)
      ? data.default_costume
      : (this.costumes[0] || {}).id || null;
    this.activeFace = null;
    this.recentFaces = [];
    this.currentEmotion = 'gentle';
    this.renderCostumeBar();
  }

  renderCostumeBar() {
    if (!this.costumeBarEl) return;
    this.costumeBarEl.hidden = this.costumes.length < 2;
    this.costumeBarEl.innerHTML = '';
    for (const c of this.costumes) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'costume-chip' + (c.id === this.currentCostume ? ' active' : '');
      btn.dataset.costume = c.id;
      const icon = document.createElement('span');
      icon.className = 'costume-chip-icon';
      icon.textContent = c.icon || '👗';
      const label = document.createElement('span');
      label.textContent = c.name || c.id;
      btn.appendChild(icon);
      btn.appendChild(label);
      btn.addEventListener('click', () => this.setCostume(c.id));
      this.costumeBarEl.appendChild(btn);
    }
    if (this.costumeIconEl) {
      const active = this.costumes.find((c) => c.id === this.currentCostume);
      this.costumeIconEl.textContent = (active && active.icon) || '👗';
    }
    if (this.costumeToggleBtn) {
      this.costumeToggleBtn.hidden = this.costumes.length < 2;
    }
  }

  /** True when this character's portrait is already on stage; reloading it would only reset it. */
  showsCharacter(profileId) {
    return Boolean(profileId) && this.loadedProfileId === profileId;
  }

  async setCharacter(charName, profileId = null) {
    const revision = ++this.characterRevision;
    this.characterRequest?.abort();
    const controller = new AbortController();
    this.characterRequest = controller;
    this.profileId = profileId || null;
    this.loadedProfileId = null;
    this.currentCharacter = charName || (this.profileId ? '当前角色' : '未绑定角色');
    this.affectionLevel = 1;
    this.currentEmotion = 'gentle';
    this.setSpeaking(false);
    this.applyPayload(null);
    if (this.charNameEl) this.charNameEl.textContent = this.currentCharacter;
    if (this.dialogueNameEl) this.dialogueNameEl.textContent = this.currentCharacter;
    if (this.emotionLabelEl) this.emotionLabelEl.textContent = '';
    if (this.dialogueTextEl) this.dialogueTextEl.textContent = '与角色聊天后，这里会显示台词。';
    this.setPortraitStatus(this.profileId ? '正在加载角色立绘…' : '请在「会话设置」选择角色；有立绘的角色包会在这里显示。');
    if (!this.profileId) return;

    let data;
    try {
      data = await this.loadPortrait(this.profileId, controller.signal);
    } catch (error) {
      if (revision === this.characterRevision && error.name !== 'AbortError') {
        this.setPortraitStatus('立绘暂时无法加载，请检查本机服务后重新打开立绘模式。');
      }
      return;
    }
    if (revision !== this.characterRevision) return;
    this.applyPayload(data);
    if (data?.character_name && !charName) {
      this.currentCharacter = data.character_name;
      if (this.charNameEl) this.charNameEl.textContent = this.currentCharacter;
      if (this.dialogueNameEl) this.dialogueNameEl.textContent = this.currentCharacter;
    }
    if (!data) {
      this.setPortraitStatus('这个角色暂时没有可用立绘。可继续聊天，或安装包含立绘的完整角色包。');
      return;
    }
    this.loadedProfileId = this.profileId;
    this.setEmotion(this.currentEmotion);
    this.preloadKeySprites();
    await this.refreshAffection();
  }

  /** 好感度阶段决定红脸差分是否可用，切换角色与每轮回复结束时刷新。 */
  async refreshAffection() {
    if (!this.profileId) return;
    const profileId = this.profileId;
    const revision = this.characterRevision;
    try {
      const data = await requestJson(`/api/affection?character_id=${encodeURIComponent(profileId)}`,
        { signal: this.characterRequest?.signal }, { timeoutMs: 15000 });
      if (revision === this.characterRevision && profileId === this.profileId) {
        this.affectionLevel = Number(data.affection_level) || 1;
      }
    } catch (_) { /* 保持上一次的好感度阶段 */ }
  }

  preloadKeySprites() {
    if (!this.enabled) return;
    const urls = [];
    for (const entries of Object.values(this.expressionSets)) {
      for (const entry of (entries || []).slice(0, 4)) {
        urls.push(this.faceUrl(entry.id));
      }
    }
    for (const url of urls) {
      if (!url || this.preloaded.has(url)) continue;
      const img = new Image();
      img.src = url;
      this.preloaded.add(url);
    }
  }

  toggleVisibility() {
    this.setVisibility(!(this.isMobile() ? this.mobileOpen : this.isVisible));
  }

  isMobile() {
    return typeof window !== 'undefined' && window.innerWidth <= 820;
  }

  dismissMobile() {
    if (!this.isMobile() || !this.mobileOpen) return false;
    this.setVisibility(false);
    return true;
  }

  setVisibility(visible) {
    if (this.isMobile()) this.mobileOpen = Boolean(visible);
    else {
      this.isVisible = Boolean(visible);
      try { localStorage.setItem('g2v_portrait_visible', String(this.isVisible)); } catch { /* visibility still works */ }
    }
    this.applyVisibility();
    if (this.isMobile()) (visible ? this.collapseBtn : this.toggleBtn)?.focus?.({ preventScroll: true });
    if (visible && !this.enabled) {
      if (this.profileId) this.setCharacter(this.currentCharacter, this.profileId);
      else this.initDefaultCharacter();
    }
  }

  applyVisibility() {
    if (!this.appEl) return;
    const mobile = this.isMobile();
    const visible = mobile ? this.mobileOpen : this.isVisible;
    if (this.stageEl) this.stageEl.inert = !visible;
    const chat = this.appEl.querySelector?.('.chat');
    const sidebar = this.appEl.querySelector?.('.sidebar');
    if (chat) chat.inert = mobile && visible;
    if (sidebar) sidebar.inert = mobile && (visible || !sidebar.classList.contains('open'));
    if (visible) {
      this.appEl.classList.remove('stage-collapsed');
      this.appEl.classList.toggle('stage-mobile-open', mobile);
      if (this.toggleBtn) {
        this.toggleBtn.classList.add('active');
        this.toggleBtn.setAttribute('aria-pressed', 'true');
      }
    } else {
      this.appEl.classList.add('stage-collapsed');
      this.appEl.classList.remove('stage-mobile-open');
      if (this.toggleBtn) {
        this.toggleBtn.classList.remove('active');
        this.toggleBtn.setAttribute('aria-pressed', 'false');
      }
    }
  }

  setCostume(costumeId) {
    if (!this.costumes.some((c) => c.id === costumeId)) return;
    this.currentCostume = costumeId;
    this.renderCostumeBar();
    // 换服装后当前表情编号仍然有效，直接按同一张脸重绘
    if (!this.faceUrl(this.activeFace)) this.activeFace = null;
    this.recentFaces = [];
    if (this.activeFace) this.paint(this.faceUrl(this.blushTier(this.activeFace)) || this.faceUrl(this.activeFace));
    else this.setEmotion(this.currentEmotion);
  }

  faceUrl(faceId) {
    if (!faceId) return '';
    const costume = this.sprites[this.currentCostume];
    const sprite = costume && costume[faceId];
    if (sprite) return sprite.file;
    return '';
  }

  firstFaceFor(emotion) {
    return this.facePool(emotion)[0]?.id || null;
  }

  facePool(emotion) {
    const available = pool => (Array.isArray(pool) ? pool : []).filter(entry => entry && this.faceUrl(entry.id));
    const requested = available(this.expressionSets[emotion]);
    if (requested.length) return requested;
    const gentle = available(this.expressionSets.gentle);
    if (gentle.length) return gentle;
    return Object.keys(this.sprites[this.currentCostume] || {}).map(id => ({ id, strength: 0 }));
  }

  /**
   * 在同一情绪的候选里挑一张脸：情绪不变时优先选与当前脸共享眉/眼/嘴部件最多的
   * 那张，所以连续说话只有细微变化，而不是整张脸跳来跳去。
   */
  pickFace(emotion) {
    const pool = this.facePool(emotion);
    if (!pool.length) return null;
    const current = this.faces[this.activeFace];
    if (!current || !pool.some((e) => e.id === this.activeFace)) {
      return pool[0].id;
    }
    const recent = new Set(this.recentFaces);
    const ranked = (skipRecent) => {
      let pick = null;
      let score = -Infinity;
      for (const entry of pool) {
        const cand = this.faces[entry.id];
        if (!cand || entry.id === this.activeFace) continue;
        if (skipRecent && recent.has(entry.id)) continue;
        // 共享部件越多，两张脸的差别越小
        const parts = Array.isArray(cand.parts) ? cand.parts : [];
        const currentParts = Array.isArray(current.parts) ? current.parts : [];
        const shared = parts.filter(p => p && currentParts.some(q => q && q.romaji === p.romaji)).length;
        const s = shared * 10 + (Number(entry.strength) || 0);
        if (s > score) {
          score = s;
          pick = entry.id;
        }
      }
      return pick;
    };
    // 最近上屏过的脸先排除，否则会在两张最相似的脸之间来回抖动
    return ranked(true) || ranked(false) || pool[0].id;
  }

  /** 红脸差分按好感度阶段解锁，害羞系情绪更早脸红。 */
  blushTier(faceId) {
    const face = this.faces[faceId];
    if (!face || !face.blush) return faceId;
    const shyish = ['shy', 'tsundere', 'angry'].includes(this.currentEmotion);
    return this.affectionLevel >= (shyish ? 2 : 3) && this.faceUrl(face.blush) ? face.blush : faceId;
  }

  /**
   * 设置立绘情绪并驱动差分选脸
   * @param {string} rawEmotion 情绪代码（gentle/happy/shy/tsundere/cool/sad/angry）
   * @param {string} [quoteText] 该句台词，用于底部台词卡
   */
  setEmotion(rawEmotion, quoteText = '') {
    const emKey = String(rawEmotion || this.currentEmotion || 'gentle').toLowerCase().trim();
    this.currentEmotion = KNOWN_EMOTION_KEYS.has(emKey) ? emKey : 'gentle';
    this.updateDialogue(quoteText);
    if (!this.enabled) return;

    const faceId = this.pickFace(this.currentEmotion);
    if (!faceId) return;
    this.recentFaces = [...this.recentFaces, this.activeFace].filter(Boolean).slice(-3);
    this.activeFace = faceId;

    const face = this.faces[faceId] || {};
    if (this.emotionLabelEl) {
      const emotionLabel = EMOTION_LABELS[this.currentEmotion] || this.currentEmotion;
      const badge = face.label ? `${emotionLabel} · ${face.label}` : emotionLabel;
      this.emotionLabelEl.textContent = badge;
      // 徽章定宽会截断，完整部件描述放 title 里悬停可看
      this.emotionLabelEl.title = face.jp ? `${badge}\n${face.jp}` : badge;
    }
    this.paint(this.faceUrl(this.blushTier(faceId)));
  }

  /** 交叉淡入淡出切换立绘（Slot A / Slot B 双层） */
  paint(targetSrc) {
    if (!targetSrc || !this.imgA || !this.imgB) return;
    this.lastPaint = { emotion: this.currentEmotion, face: this.activeFace, url: targetSrc };

    const frontImg = this.activeSlot === 'A' ? this.imgA : this.imgB;
    const backImg = this.activeSlot === 'A' ? this.imgB : this.imgA;
    const revision = ++this.paintRevision;

    // 前台已是该立绘则无需重播动画
    if (frontImg.getAttribute('src') === targetSrc && frontImg.classList.contains('is-visible')) {
      return;
    }

    let swapped = false;
    const triggerSwap = () => {
      if (swapped || revision !== this.paintRevision) return;
      swapped = true;
      backImg.classList.remove('is-hidden');
      backImg.classList.add('is-visible');
      frontImg.classList.remove('is-visible');
      frontImg.classList.add('is-hidden');
      this.activeSlot = backImg === this.imgA ? 'A' : 'B';
      this.setPortraitStatus('');
    };

    backImg.onload = triggerSwap;
    backImg.onerror = () => {
      if (revision !== this.paintRevision) return;
      const fallback = this.faceUrl(this.firstFaceFor('gentle'));
      if (fallback && backImg.getAttribute('src') !== fallback) backImg.src = fallback;
      else {
        this.applyPayload(null);
        this.setPortraitStatus('立绘图片未能加载，请检查角色包是否完整，再重新打开立绘模式。');
      }
    };
    backImg.src = targetSrc;
    if (backImg.complete && backImg.naturalWidth > 0) triggerSwap();
  }

  /**
   * 更新舞台底部台词卡片文本
   * @param {string} text 台词内容
   */
  updateDialogue(text) {
    if (this.dialogueTextEl && typeof text === 'string' && text.trim()) {
      const clean = text.trim().replace(/^[「『"“\s]+|[」』"”\s]+$/g, '');
      this.dialogueTextEl.textContent = `「${clean}」`;
    }
  }

  setPortraitStatus(message) {
    if (this.emptyStateEl) {
      this.emptyStateEl.textContent = message;
      this.emptyStateEl.hidden = !message;
    }
  }

  /** 角色发声说话状态联动（切换 is-speaking，仅柔和静态光晕，无缩放/位移动效） */
  setSpeaking(isSpeaking) {
    this.isSpeaking = Boolean(isSpeaking);
    if (this.viewportEl) {
      this.viewportEl.classList.toggle('is-speaking', this.isSpeaking);
    }
  }

  /**
   * 从消息文本或元数据推断情绪并驱动立绘（后端已给情绪时优先用它）
   */
  handleMessageEmotion(text, metaEmotion = null) {
    if (metaEmotion && KNOWN_EMOTION_KEYS.has(String(metaEmotion).toLowerCase())) {
      this.setEmotion(metaEmotion);
      return;
    }
    if (!text) return;
    const tag = String(text).match(/\[([a-zA-Z_\-]+)\]/);
    if (tag && KNOWN_EMOTION_KEYS.has(tag[1].toLowerCase())) {
      this.setEmotion(tag[1]);
      return;
    }
    for (const [emotion, pattern] of KEYWORD_EMOTIONS) {
      if (pattern.test(text)) {
        this.setEmotion(emotion);
        return;
      }
    }
  }
}

export const portraitStage = new PortraitStageController();

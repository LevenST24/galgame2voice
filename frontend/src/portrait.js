/**
 * 角色立绘舞台演出控制器 (Galgame Character Portrait Stage System)
 * 立绘完全由角色包内的表情编号差分体系（portrait/expressions.json）驱动：
 * 交叉淡入淡出、逐句同情绪微漂移、好感度解锁红脸差分、服装差分切换、发声静态光晕。
 */

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
    this.lastPaint = null;
  }

  init() {
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
    this.imgA = document.getElementById('portraitImgA');
    this.imgB = document.getElementById('portraitImgB');
    this.dialogueCardEl = document.getElementById('stageDialogueCard');
    this.dialogueTextEl = document.getElementById('stageDialogueText');
    this.dialogueNameEl = document.getElementById('stageDialogueName');

    if (!this.stageEl || !this.appEl) return;

    const savedVisible = localStorage.getItem('g2v_portrait_visible');
    if (savedVisible !== null) {
      this.isVisible = savedVisible === 'true';
    } else {
      this.isVisible = window.innerWidth >= 1200;
    }
    this.applyVisibility();

    this.bindEvents();
    this.initDefaultCharacter();
  }

  async initDefaultCharacter() {
    try {
      const res = await fetch('/api/characters');
      if (!res.ok) return;
      const data = await res.json();
      const chars = data.characters || [];
      const active = chars.find((c) => c.is_active) || chars.find((c) => c.is_default) || chars[0];
      if (active && active.name) {
        await this.setCharacter(active.name, active.id);
      }
    } catch (_) { /* 忽略，等用户切换角色时再加载 */ }
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
  }

  async loadPortrait(profileId) {
    if (!profileId) return null;
    try {
      const res = await fetch(`/api/characters/${encodeURIComponent(profileId)}/portrait`);
      if (!res.ok) return null;
      const data = await res.json();
      return data && data.enabled ? data : null;
    } catch (_) {
      return null;
    }
  }

  applyPayload(data) {
    this.enabled = Boolean(data);
    if (!data) {
      this.costumes = [];
      this.sprites = {};
      this.renderCostumeBar();
      // 该角色包没有立绘差分：收掉两张图，舞台只留名字与台词卡
      for (const img of [this.imgA, this.imgB]) {
        if (img) {
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

  async setCharacter(charName, profileId = null) {
    if (!charName) return;
    this.currentCharacter = charName;
    if (profileId) this.profileId = profileId;
    this.applyPayload(await this.loadPortrait(this.profileId));
    if (this.charNameEl) this.charNameEl.textContent = charName;
    if (this.dialogueNameEl) this.dialogueNameEl.textContent = charName;
    await this.refreshAffection();
    this.preloadKeySprites();
    this.setEmotion(this.currentEmotion);
  }

  /** 好感度阶段决定红脸差分是否可用，切换角色与每轮回复结束时刷新。 */
  async refreshAffection() {
    if (!this.profileId) return;
    try {
      const res = await fetch(`/api/affection?character_id=${encodeURIComponent(this.profileId)}`);
      if (res.ok) {
        const data = await res.json();
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
    this.setVisibility(!this.isVisible);
  }

  setVisibility(visible) {
    this.isVisible = Boolean(visible);
    localStorage.setItem('g2v_portrait_visible', String(this.isVisible));
    this.applyVisibility();
  }

  applyVisibility() {
    if (!this.appEl) return;
    if (this.isVisible) {
      this.appEl.classList.remove('stage-collapsed');
      this.appEl.classList.add('stage-mobile-open');
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
    this.paint(this.faceUrl(this.activeFace) || this.firstFaceFor(this.currentEmotion));
  }

  faceUrl(faceId) {
    if (!faceId) return '';
    const costume = this.sprites[this.currentCostume];
    const sprite = costume && costume[faceId];
    if (sprite) return sprite.file;
    for (const faces of Object.values(this.sprites)) {
      if (faces && faces[faceId]) return faces[faceId].file;
    }
    return '';
  }

  firstFaceFor(emotion) {
    const pool = this.expressionSets[emotion] || this.expressionSets.gentle || [];
    return pool.length ? pool[0].id : null;
  }

  /**
   * 在同一情绪的候选里挑一张脸：情绪不变时优先选与当前脸共享眉/眼/嘴部件最多的
   * 那张，所以连续说话只有细微变化，而不是整张脸跳来跳去。
   */
  pickFace(emotion) {
    const pool = this.expressionSets[emotion] || this.expressionSets.gentle || [];
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
        const shared = cand.parts.filter((p) => current.parts.some((q) => q.romaji === p.romaji)).length;
        const s = shared * 10 + entry.strength;
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
    return this.affectionLevel >= (shyish ? 2 : 3) ? face.blush : faceId;
  }

  /**
   * 设置立绘情绪并驱动差分选脸
   * @param {string} rawEmotion 情绪代码（gentle/happy/shy/tsundere/cool/sad/angry）
   * @param {string} [quoteText] 该句台词，用于底部台词卡
   */
  setEmotion(rawEmotion, quoteText = '') {
    const emKey = (rawEmotion || this.currentEmotion || 'gentle').toLowerCase().trim();
    this.currentEmotion = KNOWN_EMOTION_KEYS.has(emKey) ? emKey : 'gentle';
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
    if (this.dialogueTextEl && quoteText && quoteText.trim()) {
      this.dialogueTextEl.textContent = `「${quoteText.trim().replace(/^[「『"“\s]+|[」』"”\s]+$/g, '')}」`;
    }

    this.paint(this.faceUrl(this.blushTier(faceId)));
  }

  /** 交叉淡入淡出切换立绘（Slot A / Slot B 双层） */
  paint(targetSrc) {
    if (!targetSrc || !this.imgA || !this.imgB) return;
    this.lastPaint = { emotion: this.currentEmotion, face: this.activeFace, url: targetSrc };

    const frontImg = this.activeSlot === 'A' ? this.imgA : this.imgB;
    const backImg = this.activeSlot === 'A' ? this.imgB : this.imgA;

    // 前台已是该立绘则无需重播动画
    if (frontImg.src && frontImg.src.endsWith(targetSrc) && frontImg.classList.contains('is-visible')) {
      return;
    }

    let swapped = false;
    const triggerSwap = () => {
      if (swapped) return;
      swapped = true;
      backImg.classList.remove('is-hidden');
      backImg.classList.add('is-visible');
      frontImg.classList.remove('is-visible');
      frontImg.classList.add('is-hidden');
      this.activeSlot = this.activeSlot === 'A' ? 'B' : 'A';
    };

    backImg.onload = triggerSwap;
    backImg.onerror = () => {
      const fallback = this.faceUrl(this.activeFace);
      if (fallback && fallback !== targetSrc) backImg.src = fallback;
    };
    backImg.src = targetSrc;
    if (backImg.complete && backImg.naturalWidth > 0) triggerSwap();
  }

  /**
   * 更新舞台底部台词卡片文本
   * @param {string} text 台词内容
   */
  updateDialogue(text) {
    if (this.dialogueTextEl && text && text.trim()) {
      const clean = text.trim().replace(/^[「『"“\s]+|[」』"”\s]+$/g, '');
      this.dialogueTextEl.textContent = `「${clean}」`;
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

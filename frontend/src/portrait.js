/**
 * 角色立绘舞台演出控制器 (Galgame Character Portrait Stage System)
 * 实现立绘平滑交叉淡入淡出（Cross-fading）、情绪感知联动、服装差分切换与发声呼吸动画
 */

export const EMOTIONS = {
  gentle: { label: '温柔', quote: '「とりあえず、今日見たことは忘れて、わかった？」' },
  normal: { label: '平常', quote: '「……何か用でしょうか、高嶺君。」' },
  happy: { label: '开心', quote: '「今日は来てくれてありがとう。楽しかった。」' },
  smile: { label: '微笑', quote: '「ふふっ……別に笑ってないですよ。」' },
  shy: { label: '害羞', quote: '「だから、無言で凝視されると恥ずかしいんだってば……」' },
  blush: { label: '面红', quote: '「み、見ないでください……！顔が熱いだけです……」' },
  tsundere: { label: '傲娇', quote: '「今さらそんな確認しないでよ、バカ……」' },
  pout: { label: '撅嘴', quote: '「むぅ……別に拗ねてなんかいません。」' },
  angry: { label: '嫌弃', quote: '「だから気持ち悪い！なんでそんなことばっかり思いつくわけ？」' },
  cool: { label: '冷淡', quote: '「勝手に仲間にしないでください。」' },
  thinking: { label: '沉思', quote: '「……なるほど。そういうことでしたか。」' },
  surprised: { label: '惊讶', quote: '「えっ……？そ、それ本当なんですか……？」' },
  sad: { label: '感伤', quote: '「その時少し、ほんの少し、寂しいって思った……」' },
  sleepy: { label: '困倦', quote: '「ふわぁ……少し、眠くなってきてしまいました……」' },
  wink: { label: '眨眼', quote: '「……これくらい、サービスですからね。」' },
};

export const COSTUMES = {
  cafe_uniform: { name: '星光咖啡馆侍应制服', icon: '☕', short: '侍应制服' },
  casual: { name: '日常便服', icon: '👗', short: '日常便服' },
  cafe_pose_b: { name: '咖啡馆制服 (倾身姿态)', icon: '✨', short: '制服侧姿' },
};

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
    this.currentCostume = 'cafe_uniform';
    this.currentEmotion = 'gentle';
    this.isVisible = true;
    this.isSpeaking = false;
    this.currentCharacter = '四季夏目';
    this.currentCharacterId = 'natsume';
    this.preloaded = new Set();
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

    // 默认读取持久化展示状态
    const savedVisible = localStorage.getItem('g2v_portrait_visible');
    if (savedVisible !== null) {
      this.isVisible = savedVisible === 'true';
    } else {
      this.isVisible = window.innerWidth >= 1200;
    }
    this.applyVisibility();

    // 绑定事件监听
    this.bindEvents();

    // 预加载常用立绘切片
    this.preloadKeySprites();
  }

  bindEvents() {
    // 绑定顶部展开/收起按钮
    if (this.toggleBtn) {
      this.toggleBtn.addEventListener('click', () => {
        this.toggleVisibility();
      });
    }

    // 绑定舞台内收起按钮
    if (this.collapseBtn) {
      this.collapseBtn.addEventListener('click', () => {
        this.setVisibility(false);
      });
    }

    // 绑定服装选择按钮
    if (this.costumeBarEl) {
      const chips = this.costumeBarEl.querySelectorAll('.costume-chip');
      chips.forEach((btn) => {
        btn.addEventListener('click', () => {
          const costumeId = btn.dataset.costume;
          if (costumeId && COSTUMES[costumeId]) {
            this.setCostume(costumeId);
          }
        });
      });
    }

    // 快捷服装轮转
    if (this.costumeToggleBtn) {
      this.costumeToggleBtn.addEventListener('click', () => {
        const keys = Object.keys(COSTUMES);
        const nextIdx = (keys.indexOf(this.currentCostume) + 1) % keys.length;
        this.setCostume(keys[nextIdx]);
      });
    }
  }

  setCharacter(charName, charId = null) {
    if (!charName) return;
    this.currentCharacter = charName;
    if (charId) {
      this.currentCharacterId = charId;
    } else if (charName.includes('夏目') || charName.toLowerCase().includes('natsume')) {
      this.currentCharacterId = 'natsume';
    } else {
      this.currentCharacterId = charName.toLowerCase();
    }
    if (this.charNameEl) {
      this.charNameEl.textContent = charName;
    }
    if (this.dialogueNameEl) {
      this.dialogueNameEl.textContent = charName;
    }
    this.preloadKeySprites();
    this.setEmotion(this.currentEmotion);
  }

  preloadKeySprites() {
    if (!this.currentCharacter && !this.currentCharacterId) return;
    const slug = (this.currentCharacterId || this.currentCharacter || '').toLowerCase();
    if (!slug) return;
    const costumes = ['cafe_uniform', 'casual', 'cafe_pose_b'];
    const keyEmotions = ['gentle', 'happy', 'shy', 'tsundere', 'angry', 'cool'];
    for (const c of costumes) {
      for (const em of keyEmotions) {
        const url = `/static/characters/${slug}/${c}/${em}.png`;
        if (!this.preloaded.has(url)) {
          const img = new Image();
          img.src = url;
          this.preloaded.add(url);
        }
      }
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
    if (!COSTUMES[costumeId]) return;
    this.currentCostume = costumeId;

    // 更新服装图标与高亮
    if (this.costumeIconEl) {
      this.costumeIconEl.textContent = COSTUMES[costumeId].icon;
    }
    if (this.costumeToggleBtn) {
      this.costumeToggleBtn.title = `切换服装 (当前: ${COSTUMES[costumeId].name})`;
    }
    if (this.costumeBarEl) {
      this.costumeBarEl.querySelectorAll('.costume-chip').forEach((btn) => {
        btn.classList.toggle('active', btn.dataset.costume === costumeId);
      });
    }

    // 刷新当前立绘表情
    this.setEmotion(this.currentEmotion);
  }

  /**
   * 设置立绘表情与台词
   * @param {string} rawEmotion 情绪代码（如 gentle, happy, shy, tsundere 等）
   * @param {string} [quoteText] 选填台词文案（若为空则使用该表情的经典台词）
   */
  setEmotion(rawEmotion, quoteText = '') {
    const emKey = (rawEmotion || 'gentle').toLowerCase().trim();
    const meta = EMOTIONS[emKey] || EMOTIONS['gentle'] || { label: '表情', quote: '' };
    this.currentEmotion = emKey;

    if (this.emotionLabelEl) {
      this.emotionLabelEl.textContent = meta.label;
    }

    if (this.dialogueTextEl) {
      if (quoteText && quoteText.trim()) {
        const clean = quoteText.trim().replace(/^[「『"“\s]+|[」』"”\s]+$/g, '');
        this.dialogueTextEl.textContent = `「${clean}」`;
      } else if (meta.quote) {
        this.dialogueTextEl.textContent = meta.quote;
      }
    }

    // 计算资源路径
    const charSlug = (this.currentCharacterId || (this.currentCharacter ? this.currentCharacter.toLowerCase() : '')).toLowerCase();
    if (!charSlug) return;
    let targetSrc = `/static/characters/${charSlug}/${this.currentCostume}/${emKey}.png`;
    // 若 pose_b 没有该细分表情，则自动平滑回退到制服
    if (this.currentCostume === 'cafe_pose_b' && !['gentle', 'happy', 'shy', 'tsundere', 'angry', 'cool'].includes(emKey)) {
      targetSrc = `/static/characters/${charSlug}/cafe_uniform/${emKey}.png`;
    }

    // 交叉淡入淡出（Slot A / Slot B）
    const frontImg = this.activeSlot === 'A' ? this.imgA : this.imgB;
    const backImg = this.activeSlot === 'A' ? this.imgB : this.imgA;

    if (!frontImg || !backImg) return;

    // 若当前前台立绘已处于该目标立绘并且可见，无需重复触发动画
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

    // 预载并平滑交叉淡入淡出切换
    backImg.onload = () => {
      triggerSwap();
    };
    backImg.onerror = () => {
      // 容错回退到 gentle
      if (!targetSrc.endsWith('gentle.png')) {
        backImg.src = `/static/characters/${charSlug}/${this.currentCostume}/gentle.png`;
      }
    };
    backImg.src = targetSrc;
    if (backImg.complete && backImg.naturalWidth > 0) {
      triggerSwap();
    }
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

  /**
   * 角色发声说话状态联动（触发微动效与柔和呼吸光晕）
   * @param {boolean} isSpeaking 
   */
  setSpeaking(isSpeaking) {
    this.isSpeaking = Boolean(isSpeaking);
    if (this.viewportEl) {
      this.viewportEl.classList.toggle('is-speaking', this.isSpeaking);
    }
  }

  /**
   * 从消息文本或元数据中提取表情标签并驱动立绘
   * 支持 [emotion] 标签（如 [happy], [gentle], [shy], [tsundere], [cool], [sad], [angry]）
   */
  handleMessageEmotion(text, metaEmotion = null) {
    if (metaEmotion && EMOTIONS[metaEmotion.toLowerCase()]) {
      this.setEmotion(metaEmotion);
      return;
    }
    if (!text) return;

    // 匹配 [happy], [tsundere] 等前导标签
    const tagMatch = text.match(/\[([a-zA-Z_\-]+)\]/);
    if (tagMatch && tagMatch[1]) {
      const tag = tagMatch[1].toLowerCase();
      if (EMOTIONS[tag]) {
        this.setEmotion(tag);
        return;
      }
    }

    // 关键词语义快速推断（若无显式标签）
    if (/害羞|脸红|别看我|恥ずかしい|バカ/i.test(text)) {
      this.setEmotion('shy');
    } else if (/笨蛋|恶心|变态|気持ち悪い|変態|信じられない/i.test(text)) {
      this.setEmotion('angry');
    } else if (/哼|才没有|傲娇|別に|今さら/i.test(text)) {
      this.setEmotion('tsundere');
    } else if (/高兴|开心|太好了|ふふ|ありがとう|嬉しい/i.test(text)) {
      this.setEmotion('happy');
    } else if (/寂寞|难过|车祸|孤独|寂しい/i.test(text)) {
      this.setEmotion('sad');
    } else if (/冷淡|同类|无聊|勝手に/i.test(text)) {
      this.setEmotion('cool');
    }
  }
}

export const portraitStage = new PortraitStageController();

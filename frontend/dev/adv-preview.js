/**
 * adv-preview.js - Development harness for P3 ADV stage, textbox, and quick menu components.
 */

import { createStage } from '../src/adv/stage.js';
import { createTextbox } from '../src/adv/textbox.js';
import { createQuickMenu } from '../src/adv/quick_menu.js';
import { createShortcuts } from '../src/adv/shortcuts.js';
import { createLinePlayer } from '../src/adv/line_player.js';
import '../src/adv/adv.css';

// SVG Placeholder Silhouette Definitions
const SILHOUETTE_BODY_SVG = `
<svg viewBox="0 0 600 900" class="placeholder-silhouette" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="sil-grad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.9" />
      <stop offset="45%" stop-color="#818cf8" stop-opacity="0.85" />
      <stop offset="100%" stop-color="#0f172a" stop-opacity="0.95" />
    </linearGradient>
    <linearGradient id="ribbon-grad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#fde047" />
      <stop offset="100%" stop-color="#eab308" />
    </linearGradient>
  </defs>
  <!-- Wings / Back hair -->
  <path d="M 210 230 C 130 190 80 250 60 340 C 100 350 160 320 195 280 Z" fill="#7dd3fc" opacity="0.8" />
  <path d="M 390 230 C 470 190 520 250 540 340 C 500 350 440 320 405 280 Z" fill="#7dd3fc" opacity="0.8" />
  <!-- Silhouette Head & Body -->
  <path d="M 300 130 C 220 130 180 180 170 270 C 140 330 120 460 140 610 C 150 690 180 860 200 900 L 400 900 C 420 860 450 690 460 610 C 480 460 460 330 430 270 C 420 180 380 130 300 130 Z" fill="url(#sil-grad)" />
  <!-- Sailor collar and ribbon accent -->
  <path d="M 220 450 Q 300 500 380 450 L 420 620 L 180 620 Z" fill="url(#sil-grad)" opacity="0.95" />
  <path d="M 260 470 L 300 520 L 340 470" fill="none" stroke="url(#ribbon-grad)" stroke-width="4" stroke-linecap="round" />
</svg>
`;

const SILHOUETTE_FACE_SVG = `
<svg viewBox="100 110 400 420" class="placeholder-silhouette" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="face-sil-grad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.95" />
      <stop offset="50%" stop-color="#818cf8" stop-opacity="0.9" />
      <stop offset="100%" stop-color="#0f172a" stop-opacity="0.95" />
    </linearGradient>
  </defs>
  <!-- Wings silhouette crop -->
  <path d="M 210 230 C 130 190 80 250 60 340 C 100 350 160 320 195 280 Z" fill="#7dd3fc" opacity="0.85" />
  <path d="M 390 230 C 470 190 520 250 540 340 C 500 350 440 320 405 280 Z" fill="#7dd3fc" opacity="0.85" />
  <!-- Head silhouette crop -->
  <path d="M 300 130 C 220 130 180 180 170 270 C 140 330 120 460 140 610 C 150 690 180 860 200 900 L 400 900 C 420 860 450 690 460 610 C 480 460 460 330 430 270 C 420 180 380 130 300 130 Z" fill="url(#face-sil-grad)" />
  <path d="M 260 470 L 300 520 L 340 470" fill="none" stroke="#fde047" stroke-width="4" stroke-linecap="round" />
</svg>
`;

// Scripted bilingual scenario (6 lines + turn settlement into player input)
const SCRIPT = [
  {
    index: 0,
    speaker: '菲莉',
    mode: 'character',
    zh: '你好！欢迎来到视觉小说 ADV 文本框与舞台组件预览。',
    ja: 'こんにちは！ADVテキストボックスとステージコンポーネントのプレビューへようこそ。',
    audio: { status: 'ready', chunks: [{ index: 0, line: 0, audio_url: '/mock/voice-0.wav' }] },
  },
  {
    index: 1,
    speaker: '菲莉',
    mode: 'character',
    zh: '在漫长的黄昏里，夕阳的余晖悄悄穿透了活动室的窗棂，将整个房间染成一片温柔的淡金与青蓝交融之色；这正是我们在此相遇的初衷，无论前路多么漫长，故事也一定会继续向前延伸。',
    ja: '長い夕暮れの中、茜色の光がゆっくりと部室の窓を染め上げ、淡い黄金色と青が溶け合う世界が広がっていく。これが私たちの物語の始まりであり、どこまでも続いていく未来への約束なのだ。',
    audio: { status: 'ready', chunks: [{ index: 1, line: 1, audio_url: '/mock/voice-1.wav' }] },
  },
  {
    index: 2,
    speaker: '菲莉',
    mode: 'character',
    zh: '这句话由于系统的特别设定，没有配置对应的语音配音。',
    ja: 'このセリフはシステムの設定により、ボイスがスキップされています。',
    audio: { status: 'skipped', chunks: [] },
  },
  {
    index: 3,
    speaker: '菲莉',
    mode: 'character',
    zh: '语音还在后台生成中，所以文字先行打字机展现，稍后语音才姗姗来迟。',
    ja: '音声はまだ生成中なので、テキストが先に表示され、後からボイスが追いつきます。',
    audioDelay: 2000, // Arrives after audioWaitMs (1500ms)
    audio: { status: 'ready', chunks: [{ index: 3, line: 3, audio_url: '/mock/voice-3.wav' }] },
  },
  {
    index: 4,
    speaker: '',
    mode: 'narration',
    zh: '微风吹拂过窗边的帷幔，室内唯有纸页翻动的沙沙声与两人轻微的呼吸声。',
    ja: '風がカーテンをそっと揺らし、部屋には紙をめくる音と静かな吐息だけが残っていた。',
    audio: { status: 'skipped', chunks: [] },
  },
  {
    index: 5,
    speaker: '菲莉',
    mode: 'character',
    zh: '那么，能告诉我你的选择是什么吗？我想认真倾听你的想法。',
    ja: 'それで、あなたの選択を教えてもらえる？君の気持ちをちゃんと聞かせてほしいな。',
    audio: { status: 'ready', chunks: [{ index: 5, line: 5, audio_url: '/mock/voice-5.wav' }] },
  },
];

document.addEventListener('DOMContentLoaded', () => {
  const mountRoot = document.getElementById('adv-root');

  // Application Settings State
  let settings = {
    primary: 'zh',
    subtitle: 'ja',
    playerName: '旅行者',
    advWindowOpacity: 75,
    revealMsPerGrapheme: 30,
    reducedMotion: false,
    audioWaitMs: 1500,
  };

  // 1. Build Stage
  const stage = createStage(mountRoot, {
    onLetterboxClick: () => {
      // Clicks on black letterbox outside stage do not advance
    },
  });

  // Background sunset gradient
  stage.background.style.background = 'linear-gradient(175deg, #182848 0%, #2c3e6b 25%, #7a5068 55%, #b86e58 80%, #dfa07a 100%)';

  // Character sprite holder (original placeholder silhouette)
  const spriteHolder = document.createElement('div');
  spriteHolder.style.cssText = 'position: absolute; bottom: 0; left: calc(760 * var(--u)); width: calc(640 * var(--u)); height: calc(880 * var(--u)); pointer-events: none;';
  spriteHolder.innerHTML = SILHOUETTE_BODY_SVG;
  stage.sprite.appendChild(spriteHolder);

  // 2. Build Textbox inside Layer 40
  let player;

  const textbox = createTextbox(stage.textbox, {
    getSettings: () => settings,
    onAdvance: () => player?.advance(),
    onSend: (text) => {
      // Append player answer to log or restart
      setTimeout(() => {
        alert(`收到玩家回复: "${text}"。剧本将重新开始。`);
        runScript();
      }, 300);
      return true;
    },
    onMic: () => {
      alert('已点击麦克风按钮（语音输入示意）');
    },
    onAction: (act, data) => {
      if (act === 'swap_languages') {
        settings.primary = data.primary;
        settings.subtitle = data.subtitle;
      }
    },
  });

  // Place silhouette face into faceSlot
  textbox.faceSlot.innerHTML = SILHOUETTE_FACE_SVG;

  // 3. Build Quick Menu inside Layer 50
  const quickMenu = createQuickMenu(stage.quickmenu, {
    onAction: (action) => {
      switch (action) {
        case 'auto':
          player.setAuto(!player.auto);
          quickMenu.setToggle('auto', player.auto);
          break;
        case 'skip':
          player.setSkip(!player.skip);
          quickMenu.setToggle('skip', player.skip);
          break;
        case 'log':
          alert('呼出 LOG 履历面板 (功能由后续 P4 阶段完善)');
          break;
        case 'replay_voice':
          alert('语音重播 (F9)');
          break;
        case 'hide':
          toggleHide();
          break;
        case 'config':
          alert('打开系统设置面板 (F8)');
          break;
        case 'model_status':
          alert('模型连接状态良好 (GPT-4o / Qwen-Tenshi)');
          break;
      }
    },
  });
  quickMenu.setModelStatus({ label: 'Tenshi-Voice', state: 'ok' });

  // 4. Real Clock & Fake Voice Adapter
  const clock = {
    now: () => performance.now(),
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
  };

  let voiceTimer = null;
  let voiceInterval = null;

  const voice = {
    play(lineIndex, chunks) {
      if (voiceTimer) clearTimeout(voiceTimer);
      if (voiceInterval) clearInterval(voiceInterval);

      const chunkCount = chunks?.length || 1;
      const durationMs = chunkCount * 1500;
      const startTime = performance.now();

      textbox.setVoice({ active: true, progress: 0 });

      voiceInterval = setInterval(() => {
        const elapsed = performance.now() - startTime;
        const progress = Math.min(1, elapsed / durationMs);
        textbox.setVoice({ active: true, progress });
      }, 100);

      return new Promise((resolve) => {
        voiceTimer = setTimeout(() => {
          if (voiceInterval) clearInterval(voiceInterval);
          textbox.setVoice({ active: false });
          resolve('ended');
        }, durationMs);
      });
    },
    stop() {
      if (voiceTimer) {
        clearTimeout(voiceTimer);
        voiceTimer = null;
      }
      if (voiceInterval) {
        clearInterval(voiceInterval);
        voiceInterval = null;
      }
      textbox.setVoice({ active: false });
    },
  };

  // 5. Line Player
  player = createLinePlayer({
    clock,
    voice,
    render: textbox,
    paginate: (line, primaryText) => textbox.paginate(line, primaryText),
    settings,
  });
  textbox.setPlayer(player);

  // Synchronize state changes to dev toolbar badge
  const devStateEl = document.getElementById('dev-state');
  const devLineEl = document.getElementById('dev-line');
  const devPageEl = document.getElementById('dev-page');

  function updateDevStatus() {
    if (devStateEl) devStateEl.textContent = player.state;
    if (devLineEl) devLineEl.textContent = player.currentIndex !== null ? player.currentIndex : '-';
    if (devPageEl) devPageEl.textContent = player.page;

    const isInput = player.state === 'input';
    quickMenu.setEnabled('auto', !isInput);
    quickMenu.setEnabled('skip', !isInput);
    quickMenu.setEnabled('replay_voice', !isInput);
    quickMenu.setEnabled('hide', !isInput);

    quickMenu.setToggle('auto', player.auto);
    quickMenu.setToggle('skip', player.skip);
  }

  player.on('state', updateDevStatus);
  player.on('activate', updateDevStatus);

  // 6. Shortcuts
  createShortcuts(window, {
    getContext: () => ({
      inInput: player.state === 'input',
      composing: false,
      overlayOpen: false,
      stageFocused: stage.stage.contains(document.activeElement),
      stage: stage.stage,
      hidden: textbox.isHidden,
      subtitleNone: settings.subtitle === 'none',
    }),
    onAction: (act) => {
      switch (act) {
        case 'advance':
          player.advance();
          break;
        case 'toggle_auto':
          player.setAuto(!player.auto);
          updateDevStatus();
          break;
        case 'toggle_skip':
          player.setSkip(!player.skip);
          updateDevStatus();
          break;
        case 'skip-hold-start':
          player.setSkip(true);
          updateDevStatus();
          break;
        case 'skip-hold-end':
          player.setSkip(false);
          updateDevStatus();
          break;
        case 'toggle_hide':
        case 'unhide':
          toggleHide();
          break;
        case 'swap_languages':
          textbox.swapLanguages();
          break;
        case 'log':
          alert('LOG 履历面板 (快捷键生效)');
          break;
        case 'settings':
          alert('设置面板 (F8)');
          break;
      }
    },
  });

  function toggleHide() {
    const nextHidden = !textbox.isHidden;
    textbox.setHidden(nextHidden);
    quickMenu.setHidden(nextHidden);
    if (nextHidden) {
      player.hide();
    } else {
      player.unhide();
    }
  }

  // 7. Script Runner
  let activeScriptTimeouts = [];

  function clearScriptTimeouts() {
    for (const t of activeScriptTimeouts) {
      clearTimeout(t);
    }
    activeScriptTimeouts = [];
  }

  function runScript() {
    clearScriptTimeouts();
    player.reset();
    const turnId = `turn-${Date.now()}`;
    player.startTurn(turnId);

    // Push scripted lines
    SCRIPT.forEach((item) => {
      player.pushLine(turnId, {
        index: item.index,
        zh: item.zh,
        ja: item.ja,
        speaker: item.speaker,
      });

      if (item.audioDelay) {
        // Late arriving audio
        const t = setTimeout(() => {
          player.setLineAudio(turnId, item.index, item.audio);
        }, item.audioDelay);
        activeScriptTimeouts.push(t);
      } else {
        player.setLineAudio(turnId, item.index, item.audio);
      }
    });

    // Settle turn so when dialogue exhausts, it enters input mode
    player.settle(turnId, { ok: true });
    updateDevStatus();
  }

  // Hook dev toolbar buttons
  document.getElementById('dev-btn-restart')?.addEventListener('click', runScript);
  document.getElementById('dev-btn-advance')?.addEventListener('click', () => player.advance());
  document.getElementById('dev-btn-auto')?.addEventListener('click', () => {
    player.setAuto(!player.auto);
    updateDevStatus();
  });
  document.getElementById('dev-btn-skip')?.addEventListener('click', () => {
    player.setSkip(!player.skip);
    updateDevStatus();
  });
  document.getElementById('dev-btn-hide')?.addEventListener('click', toggleHide);
  document.getElementById('dev-btn-swap')?.addEventListener('click', () => textbox.swapLanguages());

  const motionBtn = document.getElementById('dev-btn-motion');
  motionBtn?.addEventListener('click', () => {
    settings.reducedMotion = !settings.reducedMotion;
    player.setSettings({ reducedMotion: settings.reducedMotion });
    motionBtn.textContent = `减弱动态: ${settings.reducedMotion ? '开' : '关'}`;
    motionBtn.classList.toggle('active', settings.reducedMotion);
  });

  const toggleToolbarBtn = document.getElementById('dev-btn-toggle-toolbar');
  const toolbarEl = document.querySelector('.dev-toolbar');
  toggleToolbarBtn?.addEventListener('click', () => {
    if (toolbarEl.style.opacity === '0.1') {
      toolbarEl.style.opacity = '1';
      toggleToolbarBtn.textContent = '折叠';
    } else {
      toolbarEl.style.opacity = '0.1';
      toggleToolbarBtn.textContent = '展开';
    }
  });

  const params = new URLSearchParams(window.location.search);
  const targetState = params.get('state');
  if (params.get('notoolbar') === '1') {
    const toolbar = document.querySelector('.dev-toolbar');
    if (toolbar) toolbar.style.display = 'none';
  }

  if (targetState === 'input') {
    clearScriptTimeouts();
    player.reset();
    const turnId = 'turn-input';
    player.startTurn(turnId);
    player.pushLine(turnId, SCRIPT[0]);
    player.setLineAudio(turnId, 0, SCRIPT[0].audio);
    player.settle(turnId, { ok: true });
    let maxAdv = 10;
    while (player.state !== 'input' && maxAdv-- > 0) {
      player.advance();
    }
    updateDevStatus();
  } else if (targetState === 'hidden') {
    clearScriptTimeouts();
    player.reset();
    const turnId = 'turn-hidden';
    player.startTurn(turnId);
    player.pushLine(turnId, SCRIPT[0]);
    player.setLineAudio(turnId, 0, SCRIPT[0].audio);
    player.advance();
    toggleHide();
    updateDevStatus();
  } else if (targetState === 'reveal') {
    clearScriptTimeouts();
    player.reset();
    const turnId = 'turn-reveal';
    player.startTurn(turnId);
    player.pushLine(turnId, SCRIPT[0]);
    player.setLineAudio(turnId, 0, SCRIPT[0].audio);
    updateDevStatus();
  } else if (targetState === 'menu') {
    clearScriptTimeouts();
    player.reset();
    const turnId = 'turn-menu';
    player.startTurn(turnId);
    player.pushLine(turnId, SCRIPT[0]);
    player.setLineAudio(turnId, 0, SCRIPT[0].audio);
    quickMenu.setRevealed(true);
    updateDevStatus();
  } else {
    // Normal interactive start
    runScript();
  }

  // Expose on window for automated screenshot tooling
  window.__adv = {
    player,
    textbox,
    quickMenu,
    runScript,
    toggleHide,
    setRevealed: (on) => quickMenu.setRevealed(on),
    setReducedMotion: (on) => {
      settings.reducedMotion = !!on;
      player.setSettings({ reducedMotion: !!on });
    },
  };
});

/**
 * ADV Sprite Preview Harness Script
 * Supports default silhouette, legacy 351x1080, and real-art packs via raw expressions.json.
 */

import { createSpriteView } from '../src/adv/sprite.js';
import { normalizePortrait, resolveSpriteState } from '../src/sprite_state.js';

// --- Synthetic Placeholder Silhouette Generator ---
function createSilhouetteSvg({ width = 800, height = 1600, expression = 'neutral', blush = false, color = '#6366f1' } = {}) {
  const blushSvg = blush ? `
    <ellipse cx="300" cy="225" rx="32" ry="16" fill="#f43f5e" opacity="0.6"/>
    <ellipse cx="500" cy="225" rx="32" ry="16" fill="#f43f5e" opacity="0.6"/>
  ` : '';

  let mouthPath = 'M375 250 Q400 265 425 250';
  if (expression === 'smile') {
    mouthPath = 'M365 245 Q400 280 435 245 Z';
  } else if (expression === 'surprised') {
    mouthPath = 'M385 240 A15 22 0 1 0 415 240 A15 22 0 1 0 385 240';
  }

  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
      <defs>
        <linearGradient id="bodyGrad" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="${color}"/>
          <stop offset="100%" stop-color="#312e81"/>
        </linearGradient>
      </defs>
      <!-- Twin tails / hair back -->
      <path d="M260 120 C180 200, 80 500, 70 950 C120 700, 200 450, 280 300 Z" fill="${color}" opacity="0.85"/>
      <path d="M540 120 C620 200, 720 500, 730 950 C680 700, 600 450, 520 300 Z" fill="${color}" opacity="0.85"/>

      <!-- Body / dress -->
      <path d="M350 330 L250 500 L210 750 L270 780 L310 540 L290 950 L510 950 L490 540 L530 780 L590 750 L550 500 L450 330 Z" fill="url(#bodyGrad)"/>
      <!-- Skirt & legs -->
      <path d="M280 900 L230 1150 L570 1150 L520 900 Z" fill="#1e1b4b"/>
      <rect x="330" y="1150" width="60" height="380" fill="#312e81" rx="10"/>
      <rect x="410" y="1150" width="60" height="380" fill="#312e81" rx="10"/>

      <!-- Neck & Head base -->
      <rect x="375" y="300" width="50" height="60" fill="#fed7aa"/>
      <path d="M290 120 C270 20, 530 20, 510 120 C520 280, 480 320, 400 320 C320 320, 280 280, 290 120 Z" fill="#fed7aa"/>

      <!-- Facial features -->
      <!-- Eyes -->
      <ellipse cx="325" cy="180" rx="20" ry="26" fill="#1e1b4b"/>
      <ellipse cx="475" cy="180" rx="20" ry="26" fill="#1e1b4b"/>
      <circle cx="330" cy="172" r="7" fill="#ffffff"/>
      <circle cx="480" cy="172" r="7" fill="#ffffff"/>

      <!-- Eyebrows -->
      <path d="M305 145 Q325 135 345 145" stroke="#4338ca" stroke-width="4" stroke-linecap="round" fill="none"/>
      <path d="M455 145 Q475 135 495 145" stroke="#4338ca" stroke-width="4" stroke-linecap="round" fill="none"/>

      <!-- Mouth -->
      <path d="${mouthPath}" stroke="#be123c" stroke-width="3" stroke-linecap="round" fill="${expression === 'smile' ? '#f43f5e' : 'none'}"/>

      <!-- Blush -->
      ${blushSvg}

      <!-- Front bangs -->
      <path d="M280 100 Q400 40 520 100 Q480 180 400 170 Q320 180 280 100 Z" fill="${color}"/>
    </svg>
  `.trim();

  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

// Synthetic legacy 351x1080 silhouette
function createLegacySilhouetteSvg() {
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" width="351" height="1080" viewBox="0 0 351 1080">
      <defs>
        <linearGradient id="legGrad" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#0284c7"/>
          <stop offset="100%" stop-color="#0f172a"/>
        </linearGradient>
      </defs>
      <rect x="0" y="0" width="351" height="1080" fill="#0284c7" opacity="0.1"/>
      <circle cx="175" cy="160" r="90" fill="#0284c7"/>
      <ellipse cx="140" cy="155" rx="10" ry="14" fill="#ffffff"/>
      <ellipse cx="210" cy="155" rx="10" ry="14" fill="#ffffff"/>
      <path d="M160 200 Q175 215 190 200" stroke="#ffffff" stroke-width="3" fill="none"/>
      <path d="M100 280 L251 280 L310 1080 L40 1080 Z" fill="url(#legGrad)"/>
      <text x="175" y="540" fill="#ffffff" font-size="22" font-family="sans-serif" text-anchor="middle" font-weight="bold">351 × 1080 Legacy</text>
    </svg>
  `.trim();
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

// Built-in synthetic packs
const SYNTHETIC_PACK = {
  enabled: true,
  character_name: '爱丽丝 (Alice)',
  default_costume: 'uniform',
  costumes: [
    {
      id: 'uniform',
      name: '日常制服',
      icon: '🎒',
      faceRect: { x: 220, y: 40, width: 360, height: 320 },
      headAnchor: { x: 400, y: 50 },
    },
    {
      id: 'casual',
      name: '夏日便服',
      icon: '👗',
      faceRect: { x: 220, y: 40, width: 360, height: 320 },
      headAnchor: { x: 400, y: 50 },
    },
  ],
  sprites: {
    uniform: {
      '01': { file: createSilhouetteSvg({ expression: 'neutral', blush: false, color: '#6366f1' }), width: 800, height: 1600 },
      '01h': { file: createSilhouetteSvg({ expression: 'neutral', blush: true, color: '#6366f1' }), width: 800, height: 1600 },
      '02': { file: createSilhouetteSvg({ expression: 'smile', blush: false, color: '#6366f1' }), width: 800, height: 1600 },
      '02h': { file: createSilhouetteSvg({ expression: 'smile', blush: true, color: '#6366f1' }), width: 800, height: 1600 },
      '03': { file: createSilhouetteSvg({ expression: 'surprised', blush: false, color: '#6366f1' }), width: 800, height: 1600 },
    },
    casual: {
      '01': { file: createSilhouetteSvg({ expression: 'neutral', blush: false, color: '#ec4899' }), width: 800, height: 1600 },
      '01h': { file: createSilhouetteSvg({ expression: 'neutral', blush: true, color: '#ec4899' }), width: 800, height: 1600 },
      '02': { file: createSilhouetteSvg({ expression: 'smile', blush: false, color: '#ec4899' }), width: 800, height: 1600 },
    },
  },
  faces: {
    '01': { label: '平静', blush: '01h', parts: [{ romaji: 'brow_a' }, { romaji: 'eye_a' }, { romaji: 'mouth_a' }] },
    '01h': { label: '脸红', parts: [{ romaji: 'brow_a' }, { romaji: 'eye_a' }, { romaji: 'mouth_a' }] },
    '02': { label: '微笑', blush: '02h', parts: [{ romaji: 'brow_a' }, { romaji: 'eye_b' }, { romaji: 'mouth_b' }] },
    '02h': { label: '开朗笑', parts: [{ romaji: 'brow_a' }, { romaji: 'eye_b' }, { romaji: 'mouth_b' }] },
    '03': { label: '惊讶', parts: [{ romaji: 'brow_c' }, { romaji: 'eye_c' }, { romaji: 'mouth_c' }] },
  },
  expression_sets: {
    gentle: [{ id: '01', strength: 2 }, { id: '02', strength: 1 }],
    happy: [{ id: '02', strength: 3 }],
    surprised: [{ id: '03', strength: 2 }],
  },
};

const LEGACY_PACK = {
  enabled: true,
  character_name: '旧版角色 (Legacy 351x1080)',
  default_costume: 'default',
  costumes: [
    {
      id: 'default',
      name: '默认立绘',
      icon: '🖼️',
    },
  ],
  sprites: {
    default: {
      '01': { file: createLegacySilhouetteSvg(), width: 351, height: 1080 },
    },
  },
  faces: {
    '01': { label: '标准', parts: [] },
  },
  expression_sets: {
    gentle: [{ id: '01', strength: 1 }],
  },
};

// ?pack= accepts an HTTP(S) directory URL or its expressions.json URL.
function packLocation(value) {
  const url = new URL(value, window.location.href);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error('角色包需要无查询参数的 HTTP(S) 目录或 expressions.json URL');
  }
  const manifest = url.pathname.endsWith('/expressions.json') ? url :
    new URL('expressions.json', url.href.replace(/\/?$/, '/'));
  return { manifest, root: new URL('./', manifest) };
}

function assetUrl(root, costume, file) {
  if (typeof costume !== 'string' || /[/\\]/.test(costume) || typeof file !== 'string' || /\\/.test(file)) {
    throw new Error('素材路径格式无效');
  }
  const segments = [costume, ...file.split('/')];
  if (segments.some(part => !part || part === '.' || part === '..')) throw new Error('素材路径不允许 .. 或空路径段');
  const url = new URL(segments.map(part => encodeURIComponent(part)).join('/'), root);
  if (url.origin !== root.origin || !url.pathname.startsWith(root.pathname)) throw new Error('素材超出角色包目录');
  return url.href;
}

// Raw v2 expressions.json adapter
function adaptV2Expressions(raw, baseUrl = '') {
  if (!raw || !raw.costumes || typeof raw.costumes !== 'object' || Array.isArray(raw.costumes) ||
      !Object.keys(raw.costumes).length) throw new Error('manifest 格式无效：缺少 costumes');
  const costumes = [];
  const sprites = {};
  const { root } = packLocation(baseUrl);

  const rawCostumes = raw.costumes || {};
  for (const [cid, c] of Object.entries(rawCostumes)) {
    const costumeMeta = {
      id: cid,
      name: c.name || cid,
      icon: c.icon || '👗',
    };
    if (c.faceRect) costumeMeta.faceRect = c.faceRect;
    if (c.headAnchor) costumeMeta.headAnchor = c.headAnchor;
    if (c.crop) costumeMeta.crop = c.crop;
    if (c.stage) costumeMeta.stage = c.stage;
    costumes.push(costumeMeta);

    sprites[cid] = {};
    const rawSprites = c.sprites || {};
    for (const [fid, sp] of Object.entries(rawSprites)) {
      const fileName = sp.file || `${fid}.webp`;
      const fileUrl = assetUrl(root, cid, fileName);
      sprites[cid][fid] = {
        file: fileUrl,
        width: sp.width,
        height: sp.height,
      };
    }
  }

  return {
    enabled: true,
    character_name: raw.pose || '白雪乃爱',
    default_costume: raw.default_costume || (costumes[0]?.id || 'uniform'),
    costumes,
    sprites,
    faces: raw.faces || {},
    expression_sets: raw.expression_sets || {},
  };
}

// --- Initialize App ---
document.addEventListener('DOMContentLoaded', async () => {
  const spriteLayer = document.getElementById('advSpriteLayer');
  const fxLayer = document.getElementById('advFxLayer');
  const faceSlot = document.getElementById('advFaceSlot');
  const statusBox = document.getElementById('statusBox');

  const packSelect = document.getElementById('packSelect');
  const costumeSelect = document.getElementById('costumeSelect');
  const emotionSelect = document.getElementById('emotionSelect');
  const faceSelect = document.getElementById('faceSelect');
  const blushSelect = document.getElementById('blushSelect');
  const motionSelect = document.getElementById('motionSelect');
  const fxSelect = document.getElementById('fxSelect');
  const reducedMotionCheck = document.getElementById('reducedMotionCheck');
  const skippingCheck = document.getElementById('skippingCheck');
  const playerTurnCheck = document.getElementById('playerTurnCheck');

  const applyBtn = document.getElementById('applyBtn');
  const clearBtn = document.getElementById('clearBtn');
  const speakerNameEl = document.getElementById('advSpeakerName');

  const view = createSpriteView({
    spriteLayer,
    fxLayer,
    faceSlot,
    onStatus(st) {
      statusBox.textContent = `状态: ${st.kind}${st.error ? ` (${st.error.message})` : ''}`;
    },
  });

  let currentRawPayload = SYNTHETIC_PACK;
  let currentModel = normalizePortrait(currentRawPayload);
  let resolvedSpriteState = null;
  let packGeneration = 0;
  let packRequest = null;
  let applyGeneration = 0;

  async function loadPack(type) {
    const generation = ++packGeneration;
    packRequest?.abort();
    const request = new AbortController();
    packRequest = request;
    applyGeneration++;
    resolvedSpriteState = null;
    currentModel = null;
    view.setModel(null);
    statusBox.textContent = '加载角色包中…';
    const current = () => generation === packGeneration && !request.signal.aborted;
    try {
      let payload;
      if (type === 'default') payload = SYNTHETIC_PACK;
      else if (type === 'legacy') payload = LEGACY_PACK;
      else if (type === 'real') {
        const urlParams = new URLSearchParams(window.location.search);
        const customPack = urlParams.get('pack') || 'http://127.0.0.1:18766';
        const { manifest } = packLocation(customPack);
        const resp = await fetch(manifest.href, { signal: request.signal });
        if (!current()) return;
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const rawJson = await resp.json();
        if (!current()) return;
        payload = adaptV2Expressions(rawJson, customPack);
      }
      if (!current()) return;
      const nextModel = normalizePortrait(payload);
      if (!nextModel) throw new Error('manifest 格式无效：没有可用角色服装');
      currentRawPayload = payload;
      currentModel = nextModel;
      view.setModel(currentModel);
      speakerNameEl.textContent = currentRawPayload.character_name || '角色';
      costumeSelect.innerHTML = '';
      for (const c of currentModel.costumes) {
        const opt = document.createElement('option');
        opt.value = c.id;
        opt.textContent = `${c.icon || ''} ${c.name || c.id}`;
        costumeSelect.appendChild(opt);
      }
      costumeSelect.value = currentModel.defaultCostume;
      updateFaceOptions();
      await applyCurrent();
    } catch (err) {
      if (!current()) return;
      const detail = err instanceof TypeError ? `网络/CORS 请求失败 (${err.message})` :
        err instanceof SyntaxError ? 'manifest JSON 格式无效' : err.message;
      statusBox.textContent = `加载外部立绘失败: ${detail}。在仓库根目录运行: node frontend/dev/serve-pack.mjs "外部角色包目录" 18766（支持 localhost CORS）`;
    }
  }

  function updateFaceOptions() {
    const costumeId = costumeSelect.value;
    const costumeSprites = currentModel?.sprites?.[costumeId] || {};
    faceSelect.innerHTML = '<option value="">自动推荐 (Auto from Emotion)</option>';
    for (const fid of Object.keys(costumeSprites)) {
      const opt = document.createElement('option');
      opt.value = fid;
      const meta = currentModel.faces?.[fid];
      opt.textContent = meta?.label ? `${fid} (${meta.label})` : fid;
      faceSelect.appendChild(opt);
    }
  }

  async function applyCurrent() {
    const generation = ++applyGeneration;
    if (playerTurnCheck.checked) {
      view.suspend();
      faceSlot.style.visibility = 'hidden';
      statusBox.textContent = '玩家回合：特写头像隐藏';
      return;
    }
    faceSlot.style.visibility = 'visible';
    if (!currentModel) return;

    const costumeId = costumeSelect.value;
    const emotion = emotionSelect.value;
    const faceId = faceSelect.value;
    const blushVal = blushSelect.value;
    const motion = motionSelect.value;
    const fx = fxSelect.value;
    const reducedMotion = reducedMotionCheck.checked;
    const skipping = skippingCheck.checked;

    let blushIntent = undefined;
    if (blushVal === 'true') blushIntent = true;
    else if (blushVal === 'false') blushIntent = false;

    const intent = {
      emotion,
      act: {
        face: faceId || undefined,
        blush: blushIntent,
        fx: fx || undefined,
      },
    };

    const ctx = {
      costume: costumeId,
      affectionLevel: 2,
    };

    resolvedSpriteState = resolveSpriteState(currentModel, resolvedSpriteState, intent, ctx);
    if (!resolvedSpriteState) {
      statusBox.textContent = '无法解析当前立绘状态';
      return;
    }

    const costumeObj = currentModel.costumes.find((c) => c.id === costumeId);

    const resolved = resolvedSpriteState;
    const result = await view.apply({
      state: resolved.state,
      faceUrl: resolved.faceUrl,
      costume: costumeObj,
    }, {
      motion,
      skipping,
      reducedMotion,
    });

    if (generation !== applyGeneration || result?.stale || result?.error) return;
    statusBox.textContent = `已绘制: 服装=${costumeId}, 脸=${resolved.state.face}, 脸红=${resolved.state.blush}, 动效=${motion || '无'}, 特效=${fx || '无'}`;
  }

  // Event bindings
  packSelect.addEventListener('change', () => loadPack(packSelect.value));
  costumeSelect.addEventListener('change', () => {
    updateFaceOptions();
    applyCurrent();
  });
  emotionSelect.addEventListener('change', applyCurrent);
  faceSelect.addEventListener('change', applyCurrent);
  blushSelect.addEventListener('change', applyCurrent);
  motionSelect.addEventListener('change', applyCurrent);
  fxSelect.addEventListener('change', applyCurrent);
  reducedMotionCheck.addEventListener('change', applyCurrent);
  skippingCheck.addEventListener('change', applyCurrent);
  playerTurnCheck.addEventListener('change', applyCurrent);

  applyBtn.addEventListener('click', applyCurrent);
  clearBtn.addEventListener('click', () => {
    applyGeneration++;
    view.clear();
    statusBox.textContent = '已清除画布';
  });

  // Check URL query parameters: ?pack=...
  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.has('pack')) {
    packSelect.value = 'real';
    await loadPack('real');
  } else {
    await loadPack('default');
  }
});

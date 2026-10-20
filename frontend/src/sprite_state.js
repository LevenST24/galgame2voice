/**
 * Pure sprite state selection and acting gates.
 * No DOM, no timers.
 */

export const EMOTION_LABELS = {
  gentle: '温柔', happy: '开心', shy: '害羞', tsundere: '傲娇',
  cool: '冷静', sad: '感伤', angry: '生气', surprised: '惊讶', thinking: '沉思',
};

export const KNOWN_EMOTION_KEYS = new Set(Object.keys(EMOTION_LABELS));

const KEYWORD_EMOTIONS = [
  ['shy', /害羞|脸红|别看我|恥ずかしい|バカ/i],
  ['angry', /生气|火大|信じられない|何様/i],
  ['tsundere', /哼|才没有|真是的|別に|今さら/i],
  ['happy', /高兴|开心|太好了|谢谢|ふふ|ありがとう|嬉しい/i],
  ['sad', /寂寞|难过|孤独|寂しい/i],
  ['cool', /无聊|随便|敷衍|勝手に/i],
];

function isFiniteNumber(val) {
  return typeof val === 'number' && Number.isFinite(val);
}

function normalizeRect(rect) {
  if (!rect || typeof rect !== 'object') return null;
  const x = rect.x;
  const y = rect.y;
  const width = rect.width;
  const height = rect.height;
  if (isFiniteNumber(x) && isFiniteNumber(y) && isFiniteNumber(width) && isFiniteNumber(height) && width > 0 && height > 0) {
    return { x, y, width, height };
  }
  return null;
}

function normalizeAnchor(anchor) {
  if (!anchor || typeof anchor !== 'object') return null;
  const x = anchor.x;
  const y = anchor.y;
  if (isFiniteNumber(x) && isFiniteNumber(y)) {
    return { x, y };
  }
  return null;
}

function normalizeCrop(crop) {
  if (!crop || typeof crop !== 'object') return null;
  const left = crop.left !== undefined ? crop.left : (crop.x !== undefined ? crop.x : null);
  const top = crop.top !== undefined ? crop.top : (crop.y !== undefined ? crop.y : null);
  const width = crop.width;
  const height = crop.height;
  if (left !== null && top !== null && isFiniteNumber(left) && isFiniteNumber(top) &&
      isFiniteNumber(width) && isFiniteNumber(height) && width > 0 && height > 0) {
    return { left, top, width, height };
  }
  return null;
}

function normalizeStage(stage) {
  if (!stage || typeof stage !== 'object') return null;
  const scale = stage.scale;
  const offset = stage.offset;
  if (!isFiniteNumber(scale) || scale <= 0 || !offset || typeof offset !== 'object') return null;
  const ox = offset.x;
  const oy = offset.y;
  if (!isFiniteNumber(ox) || !isFiniteNumber(oy)) return null;
  return { scale, offset: { x: ox, y: oy } };
}

/**
 * Turns the /api/characters/{id}/portrait payload into a model,
 * or returns null when it is missing or disabled.
 */
export function normalizePortrait(payload) {
  if (!payload || !payload.enabled) return null;

  let rawCostumes = payload.costumes;
  if (!rawCostumes) return null;
  if (typeof rawCostumes === 'object' && !Array.isArray(rawCostumes)) {
    rawCostumes = Object.entries(rawCostumes).map(([id, c]) => ({ ...(c || {}), id: c?.id || id }));
  }
  if (!Array.isArray(rawCostumes)) return null;

  const costumes = [];
  for (const c of rawCostumes) {
    if (!c || !c.id) continue;
    const entry = {
      id: String(c.id),
      name: String(c.name || c.id),
      icon: String(c.icon || '👗'),
    };
    const faceRect = normalizeRect(c.faceRect);
    if (faceRect) entry.faceRect = faceRect;

    const headAnchor = normalizeAnchor(c.headAnchor);
    if (headAnchor) entry.headAnchor = headAnchor;

    const crop = normalizeCrop(c.crop);
    if (crop) entry.crop = crop;

    const stage = normalizeStage(c.stage);
    if (stage) entry.stage = stage;

    costumes.push(entry);
  }

  if (costumes.length === 0) return null;

  const sprites = payload.sprites && typeof payload.sprites === 'object' ? payload.sprites : {};
  const faces = payload.faces && typeof payload.faces === 'object' ? payload.faces : {};
  const expressionSets = payload.expressionSets || payload.expression_sets || {};

  let defaultCostume = payload.defaultCostume || payload.default_costume;
  if (!defaultCostume || !costumes.some(c => c.id === defaultCostume)) {
    defaultCostume = costumes[0].id;
  }

  return {
    costumes,
    sprites,
    faces,
    expressionSets,
    defaultCostume,
  };
}

export function faceUrl(model, costume, faceId) {
  if (!model || !costume || !faceId) return '';
  const costumeSprites = model.sprites?.[costume];
  const sprite = costumeSprites?.[faceId];
  return sprite?.file || '';
}

export function facePool(model, costume, emotion) {
  if (!model) return [];
  const available = pool => (Array.isArray(pool) ? pool : []).filter(entry => entry && entry.id && faceUrl(model, costume, entry.id));
  const requested = available(model.expressionSets?.[emotion]);
  if (requested.length) return requested;
  const gentle = available(model.expressionSets?.gentle);
  if (gentle.length) return gentle;
  const costumeSprites = model.sprites?.[costume] || {};
  return Object.keys(costumeSprites).filter(id => costumeSprites[id]?.file).map(id => ({ id, strength: 0 }));
}

export function pickFace(model, costume, emotion, activeFace, recentFaces = []) {
  const pool = facePool(model, costume, emotion);
  if (!pool.length) return null;
  const faces = model?.faces || {};
  const current = faces[activeFace];
  if (!current || !pool.some((e) => e.id === activeFace)) {
    return pool[0].id;
  }
  const recent = new Set(recentFaces || []);
  const ranked = (skipRecent) => {
    let pick = null;
    let score = -Infinity;
    for (const entry of pool) {
      const cand = faces[entry.id];
      if (!cand || entry.id === activeFace) continue;
      if (skipRecent && recent.has(entry.id)) continue;
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
  return ranked(true) || ranked(false) || pool[0].id;
}

export function blushTier(model, costume, emotion, affectionLevel, faceId) {
  if (!model || !faceId) return faceId;
  const face = model.faces?.[faceId];
  if (!face || !face.blush) return faceId;
  const shyish = ['shy', 'tsundere', 'angry'].includes(emotion);
  const threshold = shyish ? 2 : 3;
  return ((affectionLevel || 1) >= threshold && faceUrl(model, costume, face.blush))
    ? face.blush
    : faceId;
}

export function inferEmotion(text, metaEmotion = null) {
  if (metaEmotion && KNOWN_EMOTION_KEYS.has(String(metaEmotion).toLowerCase())) {
    return String(metaEmotion).toLowerCase();
  }
  if (!text) return null;
  const tag = String(text).match(/\[([a-zA-Z_\-]+)\]/);
  if (tag && KNOWN_EMOTION_KEYS.has(tag[1].toLowerCase())) {
    return tag[1].toLowerCase();
  }
  for (const [emotion, pattern] of KEYWORD_EMOTIONS) {
    if (pattern.test(text)) {
      return emotion;
    }
  }
  return null;
}

/**
 * Resolves next sprite state given current model, previous state, intent and context.
 *
 * intent = { emotion, act: { face?, pose?, blush?, fx? } }
 * ctx = { costume, affectionLevel, recentFaces }
 */
export function resolveSpriteState(model, prev, intent, ctx = {}) {
  if (!model || !model.costumes || !model.costumes.length) return null;

  const prevState = prev?.state || prev || null;
  let costume = ctx.costume || prevState?.outfit || model.defaultCostume || model.costumes[0]?.id;
  if (!model.costumes.some(c => c.id === costume)) {
    costume = model.defaultCostume || model.costumes[0]?.id;
  }

  const costumeChanged = Boolean(prevState?.outfit && prevState.outfit !== costume);
  let recentFaces = costumeChanged ? [] : (ctx.recentFaces ? [...ctx.recentFaces] : (prev?.recentFaces ? [...prev.recentFaces] : []));

  const emotion = intent?.emotion || 'gentle';
  const act = intent?.act || {};

  let baseFace = null;

  // 1. act.face explicitly specified
  if (act.face) {
    if (faceUrl(model, costume, act.face)) {
      baseFace = act.face;
    } else {
      const match = Object.keys(model.faces || {}).find(fid =>
        model.faces[fid]?.label === act.face && faceUrl(model, costume, fid)
      );
      if (match) baseFace = match;
    }
  }

  // 2. Otherwise pick from emotion pool or follow costume/thinking rule
  if (!baseFace) {
    if (costumeChanged) {
      if (prevState?.face && faceUrl(model, costume, prevState.face)) {
        baseFace = prevState.face;
      } else {
        baseFace = pickFace(model, costume, emotion, null, []);
      }
    } else if (emotion === 'thinking') {
      const thinkingPool = (model.expressionSets?.thinking || []).filter(e => e && e.id && faceUrl(model, costume, e.id));
      if (thinkingPool.length === 0 && prevState?.face && faceUrl(model, costume, prevState.face)) {
        baseFace = prevState.face;
      } else {
        baseFace = pickFace(model, costume, emotion, prevState?.face, recentFaces);
      }
    } else {
      baseFace = pickFace(model, costume, emotion, prevState?.face, recentFaces);
    }
  }

  if (!baseFace) {
    const costumeSprites = model.sprites?.[costume] || {};
    baseFace = Object.keys(costumeSprites).find(id => costumeSprites[id]?.file) || null;
  }

  if (!baseFace) return null;

  // Update recentFaces
  let nextRecentFaces = recentFaces;
  if (prevState?.face && !costumeChanged && prevState.face !== baseFace) {
    nextRecentFaces = [...recentFaces, prevState.face].slice(-3);
  }

  // Blush resolution
  const faceMeta = model.faces?.[baseFace];
  const blushFaceId = faceMeta?.blush;
  const hasBlushVariant = Boolean(blushFaceId && faceUrl(model, costume, blushFaceId));

  let isBlushing = false;
  let effectiveFaceId = baseFace;

  if (act.blush === true) {
    if (hasBlushVariant) {
      isBlushing = true;
      effectiveFaceId = blushFaceId;
    } else {
      isBlushing = false;
      effectiveFaceId = baseFace;
    }
  } else if (act.blush === false) {
    isBlushing = false;
    effectiveFaceId = baseFace;
  } else {
    // Affection fallback
    const affFace = blushTier(model, costume, emotion, ctx.affectionLevel ?? 1, baseFace);
    if (affFace !== baseFace) {
      isBlushing = true;
      effectiveFaceId = affFace;
    } else {
      isBlushing = false;
      effectiveFaceId = baseFace;
    }
  }

  const url = faceUrl(model, costume, effectiveFaceId);
  if (!url) return null;

  const fx = act.fx || null;

  return {
    state: {
      outfit: costume,
      pose: 'default',
      face: baseFace,
      blush: isBlushing,
      fx,
    },
    faceUrl: url,
    recentFaces: nextRecentFaces,
  };
}

export function createActingGate() {
  let previousMotion = null;
  let previousFx = null;
  let lastMotionStart = null;
  const lastFxStarts = new Map();

  return {
    // Call once per committed line, including lines without acting or with
    // suppressed acting. A rejected request still becomes the preceding line.
    allowMotion(name, nowMs, enabled = true) {
      const repeated = name === previousMotion;
      previousMotion = name || null;
      if (!enabled || !name || repeated || !Number.isFinite(nowMs)) return false;
      if (lastMotionStart !== null && nowMs - lastMotionStart < 2000) return false;
      lastMotionStart = nowMs;
      return true;
    },
    allowFx(name, nowMs, enabled = true) {
      previousFx = name || null;
      if (!enabled || !previousFx || !Number.isFinite(nowMs)) return false;
      const lastStart = lastFxStarts.get(previousFx);
      if (lastStart !== undefined && nowMs - lastStart < 3000) return false;
      lastFxStarts.set(previousFx, nowMs);
      return true;
    },
    resetLine() {
      previousMotion = null;
      previousFx = null;
    },
  };
}

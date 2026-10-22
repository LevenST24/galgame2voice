import test from 'node:test';
import assert from 'node:assert/strict';
import {
  normalizePortrait,
  faceUrl,
  facePool,
  pickFace,
  blushTier,
  inferEmotion,
  resolveSpriteState,
  createActingGate,
} from '../frontend/src/sprite_state.js';

function createSampleModel() {
  const payload = {
    enabled: true,
    default_costume: 'casual',
    costumes: [
      {
        id: 'casual',
        name: 'Casual Clothes',
        icon: '👗',
        faceRect: { x: 100, y: 50, width: 300, height: 320 },
        headAnchor: { x: 250, y: 10 },
        crop: { left: 50, top: 20, width: 600, height: 1200 },
        stage: { scale: 0.9, offset: { x: 100, y: -50 } },
      },
      {
        id: 'uniform',
        name: 'School Uniform',
        icon: '🎒',
        faceRect: { x: 110, y: 60, width: 290, height: 310 },
        headAnchor: { x: 255, y: 12 },
      },
    ],
    sprites: {
      casual: {
        '01': { file: '/casual/01.png' },
        '01h': { file: '/casual/01h.png' },
        '02': { file: '/casual/02.png' },
        '03': { file: '/casual/03.png' },
        '04': { file: '/casual/04.png' },
      },
      uniform: {
        '01': { file: '/uniform/01.png' },
        '01h': { file: '/uniform/01h.png' },
        '02': { file: '/uniform/02.png' },
      },
    },
    faces: {
      '01': { label: 'Gentle Smile', blush: '01h', parts: [{ romaji: 'brow_a' }, { romaji: 'eye_a' }, { romaji: 'mouth_a' }] },
      '01h': { label: 'Blush Smile', parts: [{ romaji: 'brow_a' }, { romaji: 'eye_a' }, { romaji: 'mouth_a' }] },
      '02': { label: 'Happy Grin', parts: [{ romaji: 'brow_a' }, { romaji: 'eye_a' }, { romaji: 'mouth_b' }] },
      '03': { label: 'Shy Look', blush: '01h', parts: [{ romaji: 'brow_b' }, { romaji: 'eye_c' }, { romaji: 'mouth_c' }] },
      '04': { label: 'Surprised', parts: [{ romaji: 'brow_b' }, { romaji: 'eye_a' }, { romaji: 'mouth_b' }] },
    },
    expression_sets: {
      gentle: [{ id: '01', strength: 2 }, { id: '02', strength: 1 }],
      happy: [{ id: '02', strength: 3 }, { id: '01', strength: 1 }, { id: '04', strength: 1 }],
      shy: [{ id: '03', strength: 2 }],
    },
  };
  return normalizePortrait(payload);
}

test('normalizePortrait parses model and no art returns null', () => {
  assert.equal(normalizePortrait(null), null);
  assert.equal(normalizePortrait({}), null);
  assert.equal(normalizePortrait({ enabled: false }), null);
  assert.equal(normalizePortrait({ enabled: true, costumes: [] }), null);

  const model = createSampleModel();
  assert.ok(model);
  assert.equal(model.defaultCostume, 'casual');
  assert.equal(model.costumes.length, 2);
  assert.deepEqual(model.costumes[0].faceRect, { x: 100, y: 50, width: 300, height: 320 });
  assert.deepEqual(model.costumes[0].headAnchor, { x: 250, y: 10 });
  assert.deepEqual(model.costumes[0].stage, { scale: 0.9, offset: { x: 100, y: -50 } });

  // Invalid sizes dropped
  const invalidPayload = {
    enabled: true,
    costumes: [{
      id: 'test',
      faceRect: { x: 0, y: 0, width: -10, height: 20 },
      headAnchor: { x: 'bad', y: 0 },
      crop: { width: 0, height: 10 },
      stage: { scale: -1, offset: { x: 0, y: 0 } },
    }],
  };
  const norm = normalizePortrait(invalidPayload);
  assert.ok(norm);
  assert.equal(norm.costumes[0].faceRect, undefined);
  assert.equal(norm.costumes[0].headAnchor, undefined);
  assert.equal(norm.costumes[0].crop, undefined);
  assert.equal(norm.costumes[0].stage, undefined);

  // resolveSpriteState on null model returns null
  assert.equal(resolveSpriteState(null, null, { emotion: 'gentle' }), null);
  assert.equal(resolveSpriteState({ costumes: [] }, null, { emotion: 'gentle' }), null);
});

test('pools and fallback', () => {
  const model = createSampleModel();
  const happyPool = facePool(model, 'casual', 'happy');
  assert.equal(happyPool.length, 3);
  assert.equal(happyPool[0].id, '02');

  // Emotion without pool falls back to gentle
  const sadPool = facePool(model, 'casual', 'sad');
  assert.deepEqual(sadPool.map(e => e.id), ['01', '02']);

  // Without gentle pool falls back to costume sprite keys
  const noGentleModel = {
    ...model,
    expressionSets: {},
  };
  const fallbackPool = facePool(noGentleModel, 'casual', 'angry');
  assert.ok(fallbackPool.length > 0);
  assert.ok(fallbackPool.some(e => e.id === '01'));
});

test('drift by shared parts', () => {
  const model = createSampleModel();
  // Current face is '01' (brow_a, eye_a, mouth_a).
  // Happy pool has '02' (brow_a, eye_a, mouth_b -> shared 2),
  // '04' (brow_b, eye_a, mouth_b -> shared 1).
  const picked = pickFace(model, 'casual', 'happy', '01', []);
  assert.equal(picked, '02', 'Should pick face 02 which shares 2 parts with face 01');
});

test('recent exclusion', () => {
  const model = createSampleModel();
  // Current face is '01'. Candidates in happy pool: '02', '04'.
  // If '02' is in recentFaces, should pick '04'.
  const picked = pickFace(model, 'casual', 'happy', '01', ['02']);
  assert.equal(picked, '04', 'Should skip 02 because it is recent, picking 04');

  // If all non-active candidates are in recent, it falls back to best candidate
  const fallback = pickFace(model, 'casual', 'happy', '01', ['02', '04']);
  assert.equal(fallback, '02');
});

test('blush by affection, by act.blush:true and act.blush:false', () => {
  const model = createSampleModel();

  // 1. act.blush = true forces blush variant if available
  const resTrue = resolveSpriteState(model, null, { emotion: 'gentle', act: { face: '01', blush: true } }, { affectionLevel: 1 });
  assert.equal(resTrue.state.blush, true);
  assert.equal(resTrue.state.face, '01');
  assert.equal(resTrue.faceUrl, '/casual/01h.png');

  // 2. act.blush = false forbids blush even when affection is high
  const resFalse = resolveSpriteState(model, null, { emotion: 'shy', act: { face: '01', blush: false } }, { affectionLevel: 5 });
  assert.equal(resFalse.state.blush, false);
  assert.equal(resFalse.state.face, '01');
  assert.equal(resFalse.faceUrl, '/casual/01.png');

  // 3. Affection fallback (act.blush omitted)
  // Shy threshold is affection >= 2
  const shyAff1 = resolveSpriteState(model, null, { emotion: 'shy', act: { face: '01' } }, { affectionLevel: 1 });
  assert.equal(shyAff1.state.blush, false);
  assert.equal(shyAff1.faceUrl, '/casual/01.png');

  const shyAff2 = resolveSpriteState(model, null, { emotion: 'shy', act: { face: '01' } }, { affectionLevel: 2 });
  assert.equal(shyAff2.state.blush, true);
  assert.equal(shyAff2.faceUrl, '/casual/01h.png');

  // Gentle threshold is affection >= 3
  const gentleAff2 = resolveSpriteState(model, null, { emotion: 'gentle', act: { face: '01' } }, { affectionLevel: 2 });
  assert.equal(gentleAff2.state.blush, false);

  const gentleAff3 = resolveSpriteState(model, null, { emotion: 'gentle', act: { face: '01' } }, { affectionLevel: 3 });
  assert.equal(gentleAff3.state.blush, true);
  assert.equal(gentleAff3.faceUrl, '/casual/01h.png');
});

test('act.face by id and by label, plus an unknown value', () => {
  const model = createSampleModel();

  // By id
  const byId = resolveSpriteState(model, null, { emotion: 'gentle', act: { face: '02' } });
  assert.equal(byId.state.face, '02');
  assert.equal(byId.faceUrl, '/casual/02.png');

  // By label
  const byLabel = resolveSpriteState(model, null, { emotion: 'gentle', act: { face: 'Shy Look' } });
  assert.equal(byLabel.state.face, '03');
  assert.equal(byLabel.faceUrl, '/casual/03.png');

  // Unknown value falls back to emotion pool
  const unknown = resolveSpriteState(model, null, { emotion: 'shy', act: { face: 'nonexistent_face' } });
  assert.equal(unknown.state.face, '03'); // 'shy' pool has '03'
});

test('a costume switch keeps the face id', () => {
  const model = createSampleModel();
  const state1 = resolveSpriteState(model, null, { emotion: 'happy', act: { face: '02' } }, { costume: 'casual' });
  assert.equal(state1.state.face, '02');
  assert.equal(state1.state.outfit, 'casual');

  // Switch to uniform: 02 exists in uniform
  const state2 = resolveSpriteState(model, state1, null, { costume: 'uniform' });
  assert.equal(state2.state.face, '02');
  assert.equal(state2.state.outfit, 'uniform');
  assert.equal(state2.faceUrl, '/uniform/02.png');
  assert.deepEqual(state2.recentFaces, []);

  // Switch to uniform from a face that does not exist in uniform (03)
  const state3 = resolveSpriteState(model, null, { emotion: 'shy', act: { face: '03' } }, { costume: 'casual' });
  const state4 = resolveSpriteState(model, state3, { emotion: 'gentle' }, { costume: 'uniform' });
  assert.equal(state4.state.outfit, 'uniform');
  assert.ok(['01', '02'].includes(state4.state.face));
});

test('thinking keeps the face when no thinking pool exists', () => {
  const model = createSampleModel();
  assert.equal(model.expressionSets.thinking, undefined);

  const prev = resolveSpriteState(model, null, { emotion: 'happy', act: { face: '02' } });
  assert.equal(prev.state.face, '02');

  const next = resolveSpriteState(model, prev, { emotion: 'thinking' });
  assert.equal(next.state.face, '02', 'thinking should retain previous face when thinking pool is missing');

  // If thinking pool exists, it should use it
  const modelWithThinking = {
    ...model,
    expressionSets: {
      ...model.expressionSets,
      thinking: [{ id: '04', strength: 1 }],
    },
  };
  const nextWithPool = resolveSpriteState(modelWithThinking, prev, { emotion: 'thinking' });
  assert.equal(nextWithPool.state.face, '04');
});

test('inferEmotion matches the old rules', () => {
  // metaEmotion
  assert.equal(inferEmotion('hello', 'happy'), 'happy');
  assert.equal(inferEmotion('hello', 'TSUNDERE'), 'tsundere');
  assert.equal(inferEmotion('hello', 'INVALID'), null);

  // [emotion] bracket tags
  assert.equal(inferEmotion('[shy] 你好呀'), 'shy');
  assert.equal(inferEmotion('[angry] 哼'), 'angry');

  // keyword patterns
  assert.equal(inferEmotion('太好了，谢谢你'), 'happy');
  assert.equal(inferEmotion('何様！生气了'), 'angry');
  assert.equal(inferEmotion('别看我，害羞'), 'shy');
  assert.equal(inferEmotion('才没有呢，哼'), 'tsundere');
  assert.equal(inferEmotion('好寂寞'), 'sad');
  assert.equal(inferEmotion('真无聊'), 'cool');

  assert.equal(inferEmotion('普通的一句话'), null);
  assert.equal(inferEmotion(''), null);
  assert.equal(inferEmotion(null), null);
});

test('the acting gate: consecutive repeats, 2000 ms, 3000 ms fx', () => {
  const gate = createActingGate();

  // allowMotion: no repeat on consecutive lines
  assert.equal(gate.allowMotion('nod', 1000), true);
  assert.equal(gate.allowMotion('nod', 4000), false, 'Consecutive repeat of nod must be rejected');

  // allowMotion: minimum 2000ms from start to start
  assert.equal(gate.allowMotion('bounce', 4500), true, '3500ms since last started motion');
  assert.equal(gate.allowMotion('startle', 5000), false, 'Only 500ms since last started motion'
  );
  assert.equal(gate.allowMotion('nod', 6500), true, '2000ms since last started motion');

  // allowFx: identical fx suppressed for 3000ms
  assert.equal(gate.allowFx('sweat', 1000), true);
  assert.equal(gate.allowFx('sweat', 2500), false, 'Suppressed within 3000ms');
  assert.equal(gate.allowFx('anger', 2500), true, 'Different fx is allowed');
  assert.equal(gate.allowFx('sweat', 4000), true, '3000ms elapsed since sweat at 1000ms');
});

// ---------------------------------------------------------------------------
// v3 animation partners: only faces whose look entry has `mouth` / `blink`
// can move, so they win the ties.
// ---------------------------------------------------------------------------

function layer(id) {
  return { canvas: 'base', file: `/art/${id}.webp`, rect: { x: 0, y: 0, width: 1000, height: 2000 }, z: 10 };
}

// faces: { id: { label, mouth?, blink?, parts? } }; sets: expression_sets.
function createV3SampleModel({ faces, sets = {}, defaultFace }) {
  const layers = { body: layer('body') };
  const modelFaces = {};
  const lookFaces = {};
  for (const [id, meta] of Object.entries(faces)) {
    layers[`l_${id}`] = layer(id);
    const global = { label: meta.label };
    if (meta.parts) global.parts = meta.parts;
    modelFaces[id] = global;
    const entry = { label: meta.label, layers: [`l_${id}`] };
    if (meta.mouth) entry.mouth = { open: `${id}_open` };
    if (meta.blink) entry.blink = { closed: `${id}_closed` };
    lookFaces[id] = entry;
  }
  return normalizePortrait({
    version: 3,
    enabled: true,
    defaultCostume: 'casual',
    default_pose: 'p',
    costumes: [{ id: 'casual', name: 'Casual Clothes', icon: '👗' }],
    outfits: { casual: { name: 'Casual Clothes' } },
    poses: { p: { name: 'Standard Pose' } },
    canvases: { base: { width: 1000, height: 2000 } },
    layers,
    faces: modelFaces,
    looks: {
      'p/casual': { pose: 'p', outfit: 'casual', body: ['body'], defaultFace, faces: lookFaces },
    },
    expression_sets: sets,
  });
}

test('v3 a label shared by two faces takes the one with a mouth partner', () => {
  const model = createV3SampleModel({
    faces: {
      calm: { label: 'Happy Look' },
      grin: { label: 'Happy Look', mouth: true },
    },
    sets: { happy: [{ id: 'calm', strength: 2 }, { id: 'grin', strength: 2 }] },
  });
  const byLabel = resolveSpriteState(model, null, { emotion: 'happy', act: { face: 'Happy Look' } });
  assert.equal(byLabel.state.face, 'grin', 'the partnered face wins an equal strength');

  // A face missing from the pool counts as strength 0, yet still beats an
  // unpartnered pool entry of the same strength.
  const outside = createV3SampleModel({
    faces: {
      calm: { label: 'Happy Look' },
      grin: { label: 'Happy Look', blink: true },
    },
    sets: { happy: [{ id: 'calm', strength: 0 }] },
  });
  assert.equal(
    resolveSpriteState(outside, null, { emotion: 'happy', act: { face: 'Happy Look' } }).state.face,
    'grin',
  );
});

test('v3 a stronger face without partners beats a weaker face with partners', () => {
  const model = createV3SampleModel({
    faces: {
      grin: { label: 'Grin', mouth: true, blink: true },
      calm: { label: 'Calm' },
    },
    sets: { happy: [{ id: 'grin', strength: 1 }, { id: 'calm', strength: 3 }] },
  });
  // No current face: grin comes first in the pool.
  const opened = resolveSpriteState(model, null, { emotion: 'happy' });
  assert.equal(opened.state.face, 'calm', 'the bonus must not outrank a higher strength');
});

test('v3 with no usable current face the partnered face wins equal strengths', () => {
  const model = createV3SampleModel({
    faces: {
      plain: { label: 'Plain' },
      lively: { label: 'Lively', blink: true },
    },
    sets: { gentle: [{ id: 'plain', strength: 2 }, { id: 'lively', strength: 2 }] },
  });
  const first = resolveSpriteState(model, null, { emotion: 'gentle' });
  assert.equal(first.state.face, 'lively', 'pool order alone would keep the static face');

  // Equal strengths and no pool entry at all: the blink partner still wins.
  const noPool = createV3SampleModel({
    faces: { plain: { label: 'Plain' }, lively: { label: 'Lively', mouth: true } },
  });
  assert.equal(resolveSpriteState(noPool, null, { emotion: 'sad' }).state.face, 'lively');
});

test('v3 shared parts continuity still dominates the animation bonus', () => {
  const model = createV3SampleModel({
    faces: {
      base: { label: 'Base', parts: [{ romaji: 'eye_a' }, { romaji: 'mouth_a' }] },
      lively: { label: 'Lively', mouth: true, blink: true, parts: [{ romaji: 'eye_z' }] },
      near: { label: 'Near', parts: [{ romaji: 'eye_a' }, { romaji: 'mouth_a' }] },
    },
    sets: {
      gentle: [
        { id: 'base', strength: 0 },
        { id: 'lively', strength: 3 },
        { id: 'near', strength: 1 },
      ],
    },
  });
  const prev = { state: { outfit: 'casual', pose: 'p', face: 'base', blush: false, fx: null } };
  const kept = resolveSpriteState(model, prev, { emotion: 'gentle' });
  assert.equal(kept.state.face, 'near', 'two shared parts outweigh a stronger animated face');

  // Once continuity and strength tie, the partner breaks it.
  const tied = createV3SampleModel({
    faces: {
      base: { label: 'Base', parts: [{ romaji: 'eye_a' }, { romaji: 'mouth_a' }] },
      still: { label: 'Still', parts: [{ romaji: 'eye_a' }, { romaji: 'mouth_a' }] },
      lively: { label: 'Lively', mouth: true, parts: [{ romaji: 'eye_a' }, { romaji: 'mouth_a' }] },
    },
    sets: {
      gentle: [
        { id: 'base', strength: 0 },
        { id: 'still', strength: 2 },
        { id: 'lively', strength: 2 },
      ],
    },
  });
  assert.equal(
    resolveSpriteState(tied, prev, { emotion: 'gentle' }).state.face,
    'lively',
    'the bonus breaks a continuity tie',
  );
});

test('v2 packs keep picking without any animation bonus', () => {
  const payload = {
    enabled: true,
    default_costume: 'casual',
    costumes: [{ id: 'casual', name: 'Casual Clothes', icon: '👗' }],
    sprites: {
      casual: {
        '01': { file: '/casual/01.png' },
        '02': { file: '/casual/02.png' },
        '04': { file: '/casual/04.png' },
      },
    },
    faces: {
      '01': { label: 'Gentle Smile', parts: [{ romaji: 'eye_a' }, { romaji: 'mouth_a' }] },
      '02': { label: 'Happy Grin', parts: [{ romaji: 'eye_a' }, { romaji: 'mouth_a' }] },
      // v2 has no partner frames; stray keys must not bend the choice.
      '04': {
        label: 'Surprised',
        parts: [{ romaji: 'eye_a' }, { romaji: 'mouth_a' }],
        mouth: { open: '/casual/04_open.png' },
        blink: { closed: '/casual/04_closed.png' },
      },
    },
    expression_sets: {
      happy: [{ id: '01', strength: 2 }, { id: '02', strength: 2 }, { id: '04', strength: 2 }],
    },
  };
  const model = normalizePortrait(payload);
  assert.equal(model.version, undefined);
  assert.equal(pickFace(model, 'casual', 'happy', '01'), '02', 'ties keep the pool order');
  assert.equal(resolveSpriteState(model, null, { emotion: 'happy', act: { face: 'Surprised' } }).state.face, '04');
  assert.equal(resolveSpriteState(model, null, { emotion: 'happy' }).state.face, '01', 'no current face still takes the first pool entry');
});

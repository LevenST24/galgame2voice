import test from 'node:test';
import assert from 'node:assert/strict';
import {
  normalizePortrait,
  resolveSpriteState,
  resolveLookLayers,
} from '../frontend/src/sprite_state.js';
import { StreamAudioController } from '../frontend/src/audio_player.js';
import { pickSettings, normalizeSnapshot } from '../frontend/src/session_data.js';
import { DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL } from '../frontend/src/store.js';

// ---------------------------------------------------------------------------
// Fixture. The sprite view (layered drawing, blink, lip sync) is tested in
// tests/test_sprite_layers.mjs.
// ---------------------------------------------------------------------------

function createV3TestPayload() {
  return {
    version: 3,
    enabled: true,
    defaultCostume: 'uniform',
    default_costume: 'uniform',
    defaultPose: 'pose1',
    default_pose: 'pose1',
    costumes: [
      {
        id: 'uniform',
        name: 'School Uniform',
        icon: '🎒',
        crop: { left: 100, top: 50, width: 800, height: 1600 },
        faceRect: { x: 300, y: 100, width: 400, height: 400 },
        headAnchor: { x: 500, y: 120 },
      },
      {
        id: 'casual',
        name: 'Casual Clothes',
        icon: '👗',
        crop: { left: 100, top: 50, width: 800, height: 1600 },
        faceRect: { x: 300, y: 100, width: 400, height: 400 },
        headAnchor: { x: 500, y: 120 },
      },
    ],
    faces: {
      smile: { label: 'Smile', primary: 'gentle', emotion: 'gentle' },
      smile_blink_half: { label: 'Blink Half', primary: 'gentle', emotion: 'gentle' },
      smile_blink_closed: { label: 'Blink Closed', primary: 'gentle', emotion: 'gentle' },
      smile_mouth_half: { label: 'Mouth Half', primary: 'gentle', emotion: 'gentle' },
      smile_mouth_open: { label: 'Mouth Open', primary: 'gentle', emotion: 'gentle' },
      smile_comb_closed_open: { label: 'Combined Closed Open', primary: 'gentle', emotion: 'gentle' },
      sleepy: { label: 'Sleepy', primary: 'gentle', emotion: 'gentle' },
      plain: { label: 'Plain', primary: 'gentle', emotion: 'gentle' },
      no_half: { label: 'No Half Mouth', primary: 'gentle', emotion: 'gentle' },
      blink_only_closed: { label: 'Blink Closed Only', primary: 'gentle', emotion: 'gentle' },
      u2_smile: { label: 'U2 Smile', primary: 'gentle', emotion: 'gentle' },
      c1_smile: { label: 'C1 Smile', primary: 'gentle', emotion: 'gentle' },
    },
    canvases: {
      base: { width: 1000, height: 2000 },
    },
    outfits: {
      uniform: { name: 'School Uniform' },
      casual: { name: 'Casual Clothes' },
    },
    poses: {
      pose1: { name: 'Standard Pose' },
      pose2: { name: 'Cross Arms Pose' },
    },
    layers: {
      body_u1: { canvas: 'base', file: '/art/uniform/pose1_body.webp', rect: { x: 100, y: 50, width: 800, height: 1600 }, z: 0 },
      body_u2: { canvas: 'base', file: '/art/uniform/pose2_body.webp', rect: { x: 100, y: 50, width: 800, height: 1600 }, z: 0 },
      body_c1: { canvas: 'base', file: '/art/casual/pose1_body.webp', rect: { x: 100, y: 50, width: 800, height: 1600 }, z: 0 },
      under_layer: { canvas: 'base', file: '/art/common/shadow.webp', rect: { x: 100, y: 50, width: 800, height: 1600 }, z: -10 },
      top_layer: { canvas: 'base', file: '/art/common/highlight.webp', rect: { x: 100, y: 50, width: 800, height: 1600 }, z: 50 },
      face_smile: { canvas: 'base', file: '/art/uniform/face_smile.webp', rect: { x: 300, y: 100, width: 400, height: 400 }, z: 10 },
      face_blink_half: { canvas: 'base', file: '/art/uniform/face_blink_half.webp', rect: { x: 300, y: 100, width: 400, height: 400 }, z: 10 },
      face_blink_closed: { canvas: 'base', file: '/art/uniform/face_blink_closed.webp', rect: { x: 300, y: 100, width: 400, height: 400 }, z: 10 },
      face_mouth_half: { canvas: 'base', file: '/art/uniform/face_mouth_half.webp', rect: { x: 300, y: 100, width: 400, height: 400 }, z: 10 },
      face_mouth_open: { canvas: 'base', file: '/art/uniform/face_mouth_open.webp', rect: { x: 300, y: 100, width: 400, height: 400 }, z: 10 },
      face_combined_closed_open: { canvas: 'base', file: '/art/uniform/face_comb.webp', rect: { x: 300, y: 100, width: 400, height: 400 }, z: 10 },
      face_sleepy: { canvas: 'base', file: '/art/uniform/face_sleepy.webp', rect: { x: 300, y: 100, width: 400, height: 400 }, z: 10 },
      face_plain: { canvas: 'base', file: '/art/uniform/face_plain.webp', rect: { x: 300, y: 100, width: 400, height: 400 }, z: 10 },
      face_no_half_mouth: { canvas: 'base', file: '/art/uniform/face_no_half.webp', rect: { x: 300, y: 100, width: 400, height: 400 }, z: 10 },
      face_u2_smile: { canvas: 'base', file: '/art/uniform/face_u2_smile.webp', rect: { x: 300, y: 100, width: 400, height: 400 }, z: 10 },
      face_c1_smile: { canvas: 'base', file: '/art/casual/face_c1_smile.webp', rect: { x: 300, y: 100, width: 400, height: 400 }, z: 10 },
      blush_l: { canvas: 'base', file: '/art/common/blush.webp', rect: { x: 350, y: 200, width: 300, height: 100 }, z: 20 },
      fx_sweat: { canvas: 'base', file: '/art/common/sweat.webp', rect: { x: 320, y: 120, width: 80, height: 80 }, z: 30 },
    },
    looks: {
      'pose1/uniform': {
        pose: 'pose1',
        outfit: 'uniform',
        body: ['under_layer', 'body_u1', 'top_layer'],
        crop: { left: 100, top: 50, width: 800, height: 1600 },
        faceRect: { x: 300, y: 100, width: 400, height: 400 },
        headAnchor: { x: 500, y: 120 },
        defaultFace: 'smile',
        blush: ['blush_l'],
        fx: { sweat: ['fx_sweat'] },
        faces: {
          smile: {
            label: 'Smile',
            layers: ['face_smile'],
            blink: { half: 'smile_blink_half', closed: 'smile_blink_closed' },
            mouth: { half: 'smile_mouth_half', open: 'smile_mouth_open' },
            combinedFrames: { 'closed:open': 'smile_comb_closed_open' },
          },
          smile_blink_half: { label: 'Blink Half', layers: ['face_blink_half'] },
          smile_blink_closed: { label: 'Blink Closed', layers: ['face_blink_closed'] },
          smile_mouth_half: { label: 'Mouth Half', layers: ['face_mouth_half'] },
          smile_mouth_open: { label: 'Mouth Open', layers: ['face_mouth_open'] },
          smile_comb_closed_open: { label: 'Combined Closed Open', layers: ['face_combined_closed_open'] },
          sleepy: {
            label: 'Sleepy',
            layers: ['face_sleepy'],
            eyesClosed: true,
          },
          plain: {
            label: 'Plain',
            layers: ['face_plain'],
          },
          no_half: {
            label: 'No Half Mouth',
            layers: ['face_no_half_mouth'],
            mouth: { open: 'smile_mouth_open' },
          },
          blink_only_closed: {
            label: 'Blink Closed Only',
            layers: ['face_smile'],
            blink: { closed: 'smile_blink_closed' },
          },
        },
      },
      'pose2/uniform': {
        pose: 'pose2',
        outfit: 'uniform',
        body: ['body_u2'],
        crop: { left: 100, top: 50, width: 800, height: 1600 },
        faceRect: { x: 300, y: 100, width: 400, height: 400 },
        headAnchor: { x: 500, y: 120 },
        defaultFace: 'u2_smile',
        faces: {
          u2_smile: { label: 'U2 Smile', layers: ['face_u2_smile'] },
        },
      },
      'pose1/casual': {
        pose: 'pose1',
        outfit: 'casual',
        body: ['body_c1'],
        crop: { left: 100, top: 50, width: 800, height: 1600 },
        faceRect: { x: 300, y: 100, width: 400, height: 400 },
        headAnchor: { x: 500, y: 120 },
        defaultFace: 'c1_smile',
        faces: {
          c1_smile: { label: 'C1 Smile', layers: ['face_c1_smile'] },
        },
      },
    },
    expression_sets: {
      gentle: [{ id: 'smile', strength: 2 }],
      happy: [{ id: 'smile', strength: 3 }],
    },
  };
}

// ---------------------------------------------------------------------------
// 1. v3 Normalize and Resolution
// ---------------------------------------------------------------------------

test('R12 no matching pools uses the look default face before insertion order', () => {
  const raw = createV3TestPayload();
  const look = raw.looks['pose2/uniform'];
  look.faces = { plain: { layers: ['face_plain'] }, ...look.faces };
  const model = normalizePortrait(raw);
  for (const emotion of ['sad', 'thinking', 'happy']) {
    const result = resolveSpriteState(model, null, { emotion, act: { pose: 'pose2' } });
    assert.equal(result.state.face, 'u2_smile');
  }
  look.defaultFace = 'missing';
  const fallback = normalizePortrait(raw);
  assert.equal(resolveSpriteState(fallback, null, { emotion: 'sad', act: { pose: 'pose2' } }).state.face, 'plain');
});

test('R12 empty pools retain thinking and use gentle for other emotions', () => {
  const raw = createV3TestPayload();
  raw.expression_sets.thinking = [];
  raw.expression_sets.sad = [];
  const model = normalizePortrait(raw);
  const previous = resolveSpriteState(model, null, { act: { face: 'plain' } });
  assert.equal(resolveSpriteState(model, previous, { emotion: 'thinking' }).state.face, 'plain');
  assert.equal(resolveSpriteState(model, previous, { emotion: 'sad' }).state.face, 'smile');
});

test('v3 normalizePortrait preserves layered structures and adds version 3 metadata', () => {
  const raw = createV3TestPayload();
  const model = normalizePortrait(raw);

  assert.ok(model);
  assert.equal(model.version, 3);
  assert.equal(model.isV3, true);
  assert.equal(model.defaultCostume, 'uniform');
  assert.equal(model.defaultPose, 'pose1');
  assert.ok(model.looks['pose1/uniform']);
  assert.ok(model.poses.pose1);
  assert.ok(model.layers.body_u1);
  assert.ok(model.canvases.base);
  assert.ok(model.outfits.uniform);
});

test('v3 resolveSpriteState: sticky pose across conversation turns', () => {
  const model = normalizePortrait(createV3TestPayload());

  // 1. Initial turn: picks outfit's default look pose ('pose1')
  const s1 = resolveSpriteState(model, null, { emotion: 'gentle' });
  assert.equal(s1.state.pose, 'pose1');
  assert.equal(s1.state.outfit, 'uniform');
  assert.equal(s1.state.face, 'smile');

  // 2. Line with no pose: keeps sticky pose 'pose1'
  const s2 = resolveSpriteState(model, s1, { emotion: 'happy' });
  assert.equal(s2.state.pose, 'pose1');

  // 3. Line with explicit pose 'pose2': changes pose
  const s3 = resolveSpriteState(model, s2, { act: { pose: 'pose2' } });
  assert.equal(s3.state.pose, 'pose2');
  assert.equal(s3.state.face, 'u2_smile');

  // 4. Next line with no pose: keeps sticky pose 'pose2'
  const s4 = resolveSpriteState(model, s3, { emotion: 'gentle' });
  assert.equal(s4.state.pose, 'pose2');

  // 5. Line with invalid pose: retains previous sticky pose 'pose2'
  const s5 = resolveSpriteState(model, s4, { act: { pose: 'bad_unknown_pose' } });
  assert.equal(s5.state.pose, 'pose2');
});

test('v3 resolveSpriteState: outfit change without current pose falls back to default pose', () => {
  const model = normalizePortrait(createV3TestPayload());

  // Currently on pose2 in uniform
  const s1 = resolveSpriteState(model, null, { act: { pose: 'pose2' } });
  assert.equal(s1.state.pose, 'pose2');
  assert.equal(s1.state.outfit, 'uniform');

  // Change outfit to casual (which only has pose1/casual, no pose2/casual)
  const s2 = resolveSpriteState(model, s1, { emotion: 'gentle' }, { costume: 'casual' });
  assert.equal(s2.state.outfit, 'casual');
  assert.equal(s2.state.pose, 'pose1');
  assert.equal(s2.state.face, 'c1_smile');
});

test('v3 resolveSpriteState: missing face falls back to look defaultFace keeping pose', () => {
  const model = normalizePortrait(createV3TestPayload());
  const s1 = resolveSpriteState(model, null, { act: { face: 'non_existent_face_xyz' } });

  assert.equal(s1.state.pose, 'pose1');
  assert.equal(s1.state.face, 'smile'); // defaultFace of pose1/uniform
});

test('v3 resolveSpriteState: blush and fx per look', () => {
  const model = normalizePortrait(createV3TestPayload());

  // Explicit blush true
  const sBlush = resolveSpriteState(model, null, { act: { blush: true } });
  assert.equal(sBlush.state.blush, true);
  const blushLayer = sBlush.activeLayers.find(l => l.id === 'blush_l');
  assert.ok(blushLayer, 'blush layer should be included in activeLayers');

  // Explicit blush false
  const sNoBlush = resolveSpriteState(model, null, { act: { blush: false } }, { affectionLevel: 5 });
  assert.equal(sNoBlush.state.blush, false);
  assert.equal(sNoBlush.activeLayers.some(l => l.id === 'blush_l'), false);

  // Affection level threshold: level 3 enables blush for gentle emotion
  const sAff3 = resolveSpriteState(model, null, { emotion: 'gentle' }, { affectionLevel: 3 });
  assert.equal(sAff3.state.blush, true);

  // Affection level 1 does not enable blush
  const sAff1 = resolveSpriteState(model, null, { emotion: 'gentle' }, { affectionLevel: 1 });
  assert.equal(sAff1.state.blush, false);

  // FX per look
  const sFx = resolveSpriteState(model, null, { act: { fx: 'sweat' } });
  assert.equal(sFx.state.fx, 'sweat');
  const fxLayer = sFx.activeLayers.find(l => l.id === 'fx_sweat');
  assert.ok(fxLayer, 'look fx sweat layer should be resolved');
});

// ---------------------------------------------------------------------------
// 2. Layered Draw Order and Crop-Origin Alignment
// ---------------------------------------------------------------------------

test('resolveLookLayers sorts layers by ascending z and preserves order on tie', () => {
  const model = normalizePortrait(createV3TestPayload());
  const layers = resolveLookLayers(model, 'pose1/uniform', 'smile', true, 'sweat');

  // Verify ascending z order:
  // under_layer (z: -10), body_u1 (z: 0), face_smile (z: 10), blush_l (z: 20), fx_sweat (z: 30), top_layer (z: 50)
  const zValues = layers.map(l => l.z);
  for (let i = 1; i < zValues.length; i++) {
    assert.ok(zValues[i] >= zValues[i - 1], `z order violation at index ${i}: ${zValues[i - 1]} > ${zValues[i]}`);
  }

  assert.equal(layers[0].id, 'under_layer');
  assert.equal(layers[layers.length - 1].id, 'top_layer');
});

// ---------------------------------------------------------------------------
// 4. Lip Sync
// ---------------------------------------------------------------------------

test('lip sync: AudioPlayer node routing and RMS calculation', async () => {
  class MockAudioNode {
    constructor(type) {
      this.type = type;
      this.connections = [];
    }
    connect(target) {
      this.connections.push(target);
    }
    disconnect() {}
  }

  class MockGainNode extends MockAudioNode {
    constructor() {
      super('gain');
      this.gain = {
        value: 1,
        setValueAtTime() {},
        linearRampToValueAtTime() {},
        cancelScheduledValues() {},
      };
    }
  }

  class MockAnalyserNode extends MockAudioNode {
    constructor() {
      super('analyser');
      this.fftSize = 2048;
      this.smoothingTimeConstant = 0.8;
    }
    getFloatTimeDomainData(buffer) {
      for (let i = 0; i < buffer.length; i++) {
        buffer[i] = (i % 2 === 0 ? 0.5 : -0.5);
      }
    }
  }

  class MockAudioContext {
    constructor() {
      this.state = 'running';
      this.currentTime = 0;
      this.destination = new MockAudioNode('destination');
    }
    createGain() { return new MockGainNode(); }
    createAnalyser() { return new MockAnalyserNode(); }
    resume() { return Promise.resolve(); }
    close() { return Promise.resolve(); }
  }

  const prevWindow = globalThis.window;
  globalThis.window = { AudioContext: MockAudioContext };
  let controller = null;

  try {
    controller = new StreamAudioController();
    const ctx = controller.ensureContext();
    assert.ok(ctx);

    // Node graph verification:
    // masterGain connects to analyser
    assert.ok(controller.masterGain.connections.includes(controller.analyser));
    // masterGain does NOT connect directly to destination (no duplicate path)
    assert.equal(controller.masterGain.connections.includes(ctx.destination), false);
    // analyser connects to destination
    assert.ok(controller.analyser.connections.includes(ctx.destination));
    // Analyser properties: fftSize 256, smoothing 0.6
    assert.equal(controller.analyser.fftSize, 256);
    assert.equal(controller.analyser.smoothingTimeConstant, 0.6);

    // RMS calculation test: sqrt(0.25) = 0.5
    const rms = controller.getRms();
    assert.ok(Math.abs(rms - 0.5) < 0.001, `expected 0.5, got ${rms}`);
  } finally {
    if (controller) await controller.close();
    globalThis.window = prevWindow;
  }
});

// ---------------------------------------------------------------------------
// 6. Settings Round Trip
// ---------------------------------------------------------------------------

test('advBlink and advLipSync: store defaults, pickSettings whitelist, and snapshot normalization', () => {
  // Store defaults
  assert.equal(DEFAULT_SESSION_SETTINGS.advBlink, true);
  assert.equal(DEFAULT_SESSION_SETTINGS.advLipSync, true);

  // pickSettings preserves valid boolean values
  const picked = pickSettings({ advBlink: false, advLipSync: false }, DEFAULT_SESSION_SETTINGS);
  assert.equal(picked.advBlink, false);
  assert.equal(picked.advLipSync, false);

  // Invalid types fall back to defaults
  const invalidInputs = [
    { advBlink: 'false', advLipSync: 0 },
    { advBlink: null, advLipSync: undefined },
    { advBlink: 1, advLipSync: 'true' },
  ];
  for (const input of invalidInputs) {
    const res = pickSettings(input, DEFAULT_SESSION_SETTINGS);
    assert.equal(res.advBlink, true);
    assert.equal(res.advLipSync, true);
  }

  // normalizeSnapshot round-trip
  const snapshot = {
    sessions: [
      {
        id: 's1',
        title: 'Test',
        messages: [],
        settings: {
          advBlink: false,
          advLipSync: false,
        },
      },
    ],
  };
  const normalized = normalizeSnapshot(snapshot, DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL);
  assert.equal(normalized.sessions[0].settings.advBlink, false);
  assert.equal(normalized.sessions[0].settings.advLipSync, false);
});

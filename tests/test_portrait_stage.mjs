import assert from 'node:assert/strict';
import { PortraitStageController, portraitStage } from '../frontend/src/portrait.js';
import { ensureSessionVoice, initVoiceSettings } from '../frontend/src/controllers/voice_settings.js';
import { state } from '../frontend/src/store.js';

class Element {
  constructor() {
    const names = new Set(['is-hidden']);
    this.classList = { add: (...items) => items.forEach(item => names.add(item)),
      remove: (...items) => items.forEach(item => names.delete(item)), contains: item => names.has(item),
      toggle: (item, on) => on ? names.add(item) : names.delete(item) };
    this.textContent = ''; this.src = ''; this.children = []; this.dataset = {};
    this.complete = false; this.naturalWidth = 0;
  }
  set innerHTML(value) { this.children = []; }
  appendChild(child) { this.children.push(child); }
  addEventListener() {}
  setAttribute(name, value) { this[name] = value; }
  getAttribute(name) { return this[name] || null; }
  removeAttribute(name) { delete this[name]; }
}
globalThis.document = { createElement: () => new Element(), getElementById: () => null };
globalThis.Image = Element;
globalThis.localStorage = { getItem: () => null, setItem() {} };
const response = data => ({ ok: true, json: async () => data });
const payload = name => ({ enabled: true, character_name: name, default_costume: 'casual',
  costumes: [{ id: 'casual', name: 'Casual' }],
  sprites: { casual: { gentle: { file: `/${name}/gentle.png` }, happy: { file: `/${name}/happy.png` } } },
  faces: { gentle: { label: 'Calm', parts: [] }, happy: { label: 'Happy', parts: [] } },
  expression_sets: { gentle: [{ id: 'gentle', strength: 2 }], happy: [{ id: 'happy', strength: 2 }] } });
function stage() {
  const controller = new PortraitStageController();
  for (const name of ['imgA', 'imgB', 'charNameEl', 'dialogueNameEl', 'dialogueTextEl',
    'emotionLabelEl', 'emptyStateEl', 'costumeBarEl', 'costumeToggleBtn', 'costumeIconEl']) {
    controller[name] = new Element();
  }
  return controller;
}
let passed = 0;
const tests = [];
function test(name, run) { tests.push({ name, run }); }

test('loadPortrait uses legacy whole images from a v3 pack', async () => {
  const controller = stage();
  const legacy = payload('Legacy');
  globalThis.fetch = async () => response({ enabled: true, version: 3, sprites: {},
    layers: { body: {} }, legacy });
  const loaded = await controller.loadPortrait(1);
  assert.deepEqual(loaded, legacy);
  controller.applyPayload(loaded);
  assert.equal(controller.faceUrl('gentle'), '/Legacy/gentle.png');
});

test('loadPortrait returns null for v3 without a legacy pack', async () => {
  globalThis.fetch = async () => response({ enabled: true, version: 3, sprites: {}, layers: {} });
  assert.equal(await stage().loadPortrait(1), null);
});

test('a desktop portrait preference does not cover the chat when the window becomes narrow', () => {
  const controller = stage();
  controller.appEl = new Element(); controller.stageEl = new Element(); controller.toggleBtn = new Element();
  const previousWindow = globalThis.window;
  globalThis.window = { innerWidth: 390 };
  try {
    controller.isVisible = true;
    controller.applyVisibility();
    assert.equal(controller.appEl.classList.contains('stage-mobile-open'), false);
    assert.equal(controller.stageEl.inert, true);
    assert.equal(controller.toggleBtn['aria-pressed'], 'false');
    controller.enabled = true;
    controller.toggleVisibility();
    assert.equal(controller.appEl.classList.contains('stage-mobile-open'), true);
    assert.equal(controller.stageEl.inert, false);
    assert.equal(controller.dismissMobile(), true);
    assert.equal(controller.stageEl.inert, true);
    assert.equal(controller.isVisible, true);
    globalThis.window.innerWidth = 1280;
    controller.applyVisibility();
    assert.equal(controller.appEl.classList.contains('stage-collapsed'), false);
    assert.equal(controller.stageEl.inert, false);
  } finally { if (previousWindow === undefined) delete globalThis.window; else globalThis.window = previousWindow; }
});

test('late character response cannot replace the selected character', async () => {
  const controller = stage();
  let finishOld;
  globalThis.fetch = async url => url.includes('/1/portrait')
    ? new Promise(resolve => { finishOld = resolve; })
    : response(url.includes('/portrait') ? payload('New') : { affection_level: 1 });
  const old = controller.setCharacter('Old', 1);
  await controller.setCharacter('New', 2);
  finishOld(response(payload('Old')));
  await old;
  assert.equal(controller.currentCharacter, 'New');
  assert.equal(controller.profileId, 2);
  assert.equal(controller.faceUrl('gentle'), '/New/gentle.png');
  assert.equal(controller.charNameEl.textContent, 'New');
});

test('clearing the character removes its old images and profile ID', async () => {
  const controller = stage();
  globalThis.fetch = async url => response(url.includes('/portrait') ? payload('Old') : { affection_level: 3 });
  await controller.setCharacter('Old', 1);
  controller.imgB.onload();
  await controller.setCharacter('', null);
  assert.equal(controller.profileId, null);
  assert.equal(controller.enabled, false);
  assert.equal(controller.imgA.classList.contains('is-visible'), false);
  assert.equal(controller.imgB.classList.contains('is-visible'), false);
  assert.equal(controller.affectionLevel, 1);
});

test('a late image load cannot restore the previous character', async () => {
  const controller = stage();
  globalThis.fetch = async url => response(url.includes('/1/') ? payload('Old')
    : url.includes('/portrait') ? { enabled: false } : { affection_level: 1 });
  await controller.setCharacter('Old', 1);
  const oldLoad = controller.imgB.onload;
  await controller.setCharacter('Without sprites', 2);
  oldLoad();
  assert.equal(controller.imgB.classList.contains('is-visible'), false);
  assert.equal(controller.emptyStateEl.hidden, false);
});

test('costume switch stays in the selected costume when a face is unavailable', async () => {
  const controller = stage();
  const data = payload('Old');
  data.costumes.push({ id: 'uniform', name: 'Uniform' });
  data.sprites.uniform = { gentle: { file: '/uniform/gentle.png' } };
  controller.applyPayload(data);
  controller.setEmotion('happy'); controller.imgB.onload();
  controller.setCostume('uniform');
  assert.equal(controller.currentCostume, 'uniform');
  assert.equal(controller.lastPaint.url, '/uniform/gentle.png');
});

test('no portrait still shows the current dialogue and an actionable state', async () => {
  const controller = stage();
  globalThis.fetch = async () => response({ enabled: false });
  await controller.setCharacter('No portrait', 2);
  controller.setEmotion('happy', '谢谢你');
  assert.equal(controller.dialogueTextEl.textContent, '「谢谢你」');
  assert.equal(controller.emptyStateEl.hidden, false);
  assert.match(controller.emptyStateEl.textContent, /立绘/);
});

test('default initialization cannot override an explicit session selection', async () => {
  const controller = stage();
  let finishList;
  globalThis.fetch = async url => url === '/api/characters'
    ? new Promise(resolve => { finishList = resolve; })
    : response(url.includes('/portrait') ? payload('Session') : { affection_level: 1 });
  const initial = controller.initDefaultCharacter();
  await controller.setCharacter('Session', 2);
  finishList(response({ characters: [{ id: 1, name: 'Global', is_active: true }] }));
  await initial;
  assert.equal(controller.profileId, 2);
  assert.equal(controller.charNameEl.textContent, 'Session');
});

test('an offline voice engine does not prevent the session portrait from loading', async () => {
  Object.assign(portraitStage, stage());
  initVoiceSettings({});
  const session = { id: 'offline', settings: { voiceProfileId: 2 } };
  state.sessions = [session]; state.activeId = session.id;
  globalThis.fetch = async url => {
    if (url.endsWith('/profiles')) return response({ profiles: [{ id: 2, name: 'Session' }], active_profile_id: null });
    if (url.endsWith('/switch')) return { ok: false, status: 503, json: async () => ({ detail: '引擎未启动' }) };
    return response(url.includes('/portrait') ? payload('Session') : { affection_level: 1 });
  };
  await ensureSessionVoice(session);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(portraitStage.enabled, true);
  assert.equal(portraitStage.profileId, 2);
  assert.equal(portraitStage.faceUrl('gentle'), '/Session/gentle.png');
});

test('late engine switch completion cannot change the newer session portrait', async () => {
  Object.assign(portraitStage, stage());
  initVoiceSettings({});
  const old = { id: 'old', settings: { voiceProfileId: 2 } };
  const current = { id: 'new', settings: { voiceProfileId: 3 } };
  state.sessions = [old, current]; state.activeId = old.id;
  let finishSwitch;
  globalThis.fetch = async (url, options) => {
    if (url.endsWith('/profiles')) return response({ profiles: [{ id: 2 }, { id: 3 }], active_profile_id: 2 });
    if (url.endsWith('/switch')) {
      return JSON.parse(options.body).profile_id === 2
        ? new Promise(resolve => { finishSwitch = resolve; }) : response({ profile: 'New' });
    }
    return response(url.includes('/portrait') ? payload(url.includes('/2/') ? 'Old' : 'New') : { affection_level: 1 });
  };
  const pending = ensureSessionVoice(old);
  while (!finishSwitch) await new Promise(resolve => setTimeout(resolve, 0));
  state.activeId = current.id;
  await ensureSessionVoice(current);
  finishSwitch(response({ profile: 'Old' }));
  await pending;
  assert.equal(portraitStage.profileId, 3);
  assert.equal(portraitStage.faceUrl('gentle'), '/New/gentle.png');
});

test('portrait connection errors give a retryable state and reopening retries', async () => {
  const controller = stage();
  let calls = 0;
  globalThis.fetch = async () => { calls++; throw new TypeError('Failed to fetch'); };
  await controller.setCharacter('Session', 2);
  assert.match(controller.emptyStateEl.textContent, /重新打开/);
  globalThis.fetch = async url => { calls++; return response(url.includes('/portrait') ? payload('Session') : {}); };
  controller.setVisibility(true);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(controller.enabled, true);
  assert.ok(calls >= 2);
});

test('failed sprite images clear broken images and can be retried by reopening', async () => {
  const controller = stage();
  globalThis.fetch = async url => response(url.includes('/portrait') ? payload('Session') : {});
  await controller.setCharacter('Session', 2);
  controller.imgB.onerror();
  assert.equal(controller.enabled, false);
  assert.equal(controller.imgB.getAttribute('src'), null);
  assert.match(controller.emptyStateEl.textContent, /图片未能加载/);
  controller.setVisibility(true);
  await new Promise(resolve => setTimeout(resolve, 0));
  controller.imgB.onload();
  assert.equal(controller.enabled, true);
  assert.equal(controller.emptyStateEl.hidden, true);
});

test('continuous multi-character and multi-emotion marathon switches cleanly without cross-talk', async () => {
  const controller = stage();
  const characters = ['CharA', 'CharB', 'CharC', 'CharD'];
  const emotions = ['gentle', 'happy', 'shy', 'tsundere', 'cool', 'sad', 'angry'];
  globalThis.fetch = async url => {
    const m = /\/api\/characters\/(\d+)\/portrait/.exec(url);
    if (m) {
      const idx = (Number(m[1]) - 1) % characters.length;
      const name = characters[idx];
      const p = payload(name);
      for (const em of emotions) {
        p.sprites.casual[`${em}_1`] = { file: `/${name}/${em}_1.png`, blush: `${em}_blush` };
        p.sprites.casual[`${em}_2`] = { file: `/${name}/${em}_2.png` };
        p.sprites.casual[`${em}_blush`] = { file: `/${name}/${em}_blush.png` };
        p.faces[`${em}_1`] = { label: em, blush: `${em}_blush`, parts: [{ romaji: 'eye_a' }] };
        p.faces[`${em}_2`] = { label: em, parts: [{ romaji: 'eye_a' }, { romaji: 'mouth_b' }] };
        p.faces[`${em}_blush`] = { label: `${em}-blush`, parts: [{ romaji: 'eye_a' }] };
        p.expression_sets[em] = [{ id: `${em}_1`, strength: 2 }, { id: `${em}_2`, strength: 1 }];
      }
      return response(p);
    }
    return response({ affection_level: 3 });
  };

  for (let round = 0; round < 12; round++) {
    const profileId = (round % characters.length) + 1;
    const expectedName = characters[profileId - 1];
    await controller.setCharacter(expectedName, profileId);
    assert.equal(controller.currentCharacter, expectedName);
    assert.equal(controller.profileId, profileId);
    for (let step = 0; step < emotions.length; step++) {
      const em = emotions[(round + step) % emotions.length];
      const line = `${expectedName} line ${round}-${step}`;
      controller.setEmotion(em, line);
      const backSlot = controller.activeSlot === 'A' ? controller.imgB : controller.imgA;
      backSlot.onload?.();
      assert.equal(controller.dialogueTextEl.textContent, `「${line}」`);
      assert.ok(controller.lastPaint.url.startsWith(`/${expectedName}/`));
    }
  }
});

test('saving the same session keeps its portrait, line and costume; switching sessions reloads', async () => {
  Object.assign(portraitStage, stage());
  initVoiceSettings({});
  const session = { id: 'same', settings: { voiceProfileId: 2 } };
  state.sessions = [session]; state.activeId = session.id;
  let portraitLoads = 0;
  globalThis.fetch = async url => {
    if (url.endsWith('/profiles')) return response({ profiles: [{ id: 2, name: 'Session' }], active_profile_id: 2 });
    if (url.endsWith('/switch')) return response({ profile: 'Session' });
    if (url.includes('/portrait')) { portraitLoads++; return response(payload('Session')); }
    return response({ affection_level: 1 });
  };
  const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setTimeout(resolve, 0)); };
  await ensureSessionVoice(session); await settle();
  assert.equal(portraitStage.showsCharacter(2), true);
  portraitStage.dialogueTextEl.textContent = '「最后一句」';

  await ensureSessionVoice(session, { keepPortrait: true }); await settle();
  assert.equal(portraitLoads, 1);
  assert.equal(portraitStage.dialogueTextEl.textContent, '「最后一句」');

  await ensureSessionVoice(session); await settle();
  assert.equal(portraitLoads, 2);
  assert.notEqual(portraitStage.dialogueTextEl.textContent, '「最后一句」');
});

test('a failed portrait load is not treated as on stage', async () => {
  const controller = stage();
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
  await controller.setCharacter('Session', 2);
  assert.equal(controller.showsCharacter(2), false);
  globalThis.fetch = async url => response(url.includes('/portrait') ? payload('Session') : {});
  await controller.setCharacter('Session', 2);
  assert.equal(controller.showsCharacter(2), true);
  assert.equal(controller.showsCharacter(3), false);
});

for (const { name, run } of tests) {
  try { await run(); passed++; console.log(`PASS: ${name}`); }
  catch (error) { console.error(`FAIL: ${name}`, error); process.exitCode = 1; }
}
console.log(`Portrait stage: ${passed}/${tests.length} scenarios passed`);

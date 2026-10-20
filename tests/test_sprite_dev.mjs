import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
import { normalizePortrait, resolveSpriteState } from '../frontend/src/sprite_state.js';

const source = await readFile(new URL('../frontend/dev/sprite-preview.js', import.meta.url), 'utf8');
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const raw = { pose: 'Remote', costumes: { remote: { sprites: { '01': { file: 'face.webp' } } } } };
async function harness(fetch) {
  const controls = new Map();
  let ready;
  const calls = { models: [], applies: [], suspends: 0, clears: 0, reducerInputs: [] };
  const get = id => {
    if (!controls.has(id)) controls.set(id, { value: '', checked: false, style: {}, textContent: '', children: [],
      listeners: new Map(), addEventListener(event, fn) { this.listeners.set(event, fn); },
      appendChild(child) { this.children.push(child); } });
    return controls.get(id);
  };
  get('emotionSelect').value = 'gentle';
  const view = { setModel(model) { calls.models.push(model); }, clear() { calls.clears++; },
    suspend() { calls.suspends++; }, async apply(input) { calls.applies.push(input); return { stale: false }; } };
  const context = vm.createContext({ URL, URLSearchParams, AbortController, console, fetch,
    window: { location: { search: '?pack=http://127.0.0.1:18766', href: 'http://localhost:3015/static/dev/sprite-preview.html' } },
    document: { addEventListener(name, fn) { ready = fn; }, getElementById: get, createElement: () => ({}) },
    createSpriteView: () => view, normalizePortrait, resolveSpriteState: (...args) => {
      calls.reducerInputs.push(args); return resolveSpriteState(...args);
    } });
  // Execute the whole entry module with only its two static imports supplied
  // by the test. This exercises its actual DOM listeners and async load path.
  new vm.Script(source.replace(/^import .*;$/gm, ''), { filename: 'sprite-preview.js' }).runInContext(context);
  return { context, calls, get, start: ready, change: type => {
    get('packSelect').value = type;
    return get('packSelect').listeners.get('change')();
  } };
}

for (const phase of ['fetch', 'json']) {
  test(`pack switching ignores a stale real pack during ${phase} and aborts its request`, async () => {
    const response = deferred(), json = deferred();
    let requestOptions;
    const h = await harness((url, options) => { requestOptions = options; return response.promise; });
    const old = h.start();
    if (phase === 'json') { response.resolve({ ok: true, json: () => json.promise }); await Promise.resolve(); }
    await h.change('legacy');
    if (phase === 'fetch') response.resolve({ ok: true, json: async () => raw });
    else json.resolve(raw);
    await old;
    assert.equal(h.calls.models.at(-1).costumes[0].id, 'default');
    assert.equal(requestOptions.signal.aborted, true);
    assert.match(h.get('advSpeakerName').textContent, /Legacy/);
  });
}

test('pack switches reset resolved state, so the previous costume face cannot leak', async () => {
  const h = await harness(async () => ({ ok: true, json: async () => raw }));
  await h.start();
  await h.change('default');
  assert.equal(h.calls.applies.at(-1).state.outfit, 'uniform');
  assert.equal(h.calls.reducerInputs.at(-1)[1], null, 'new pack starts without previous resolved state');
});

test('player turn invokes renderer suspension to stop pending work and acting', async () => {
  const h = await harness(async () => ({ ok: true, json: async () => raw }));
  await h.start(); h.get('playerTurnCheck').checked = true;
  await h.get('playerTurnCheck').listeners.get('change')();
  assert.equal(h.calls.suspends, 1);
  assert.equal(h.get('advFaceSlot').style.visibility, 'hidden');
});

test('adapter percent-encodes every file path segment and costume id', async () => {
  const h = await harness();
  const value = h.context.adaptV2Expressions({ costumes: { '衣#?': { sprites: {
    '01': { file: 'sub dir/face#1?.webp' } } } } }, 'http://127.0.0.1:18766/pack/');
  assert.equal(value.sprites['衣#?']['01'].file,
    'http://127.0.0.1:18766/pack/%E8%A1%A3%23%3F/sub%20dir/face%231%3F.webp');
});

test('adapter refuses parent segments and costume traversal', async () => {
  const h = await harness();
  for (const [id, file] of [['../escape', 'face.webp'], ['one', '../face.webp'], ['one', 'a/../../face.webp']]) {
    assert.throws(() => h.context.adaptV2Expressions({ costumes: { [id]: { sprites: { '01': { file } } } } },
      'http://127.0.0.1:18766/pack/'));
  }
});

test('real-pack error explains the dev CORS server command', async () => {
  const h = await harness(async () => { throw new TypeError('Failed to fetch'); });
  await h.start();
  assert.match(h.get('statusBox').textContent, /node frontend\/dev\/serve-pack\.mjs/);
  assert.match(h.get('statusBox').textContent, /CORS/);
});

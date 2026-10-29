import assert from 'node:assert/strict';
import test from 'node:test';
import { state, getActive, DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { ensureSessionVoice, fetchVoiceProfiles, getActiveProfileId, getVoiceProfiles,
  initVoiceSettings } from '../frontend/src/controllers/voice_settings.js';
import { setup } from './helpers/adv_main.mjs';

const tick = () => new Promise(resolve => setImmediate(resolve));
const response = (data, status = 200) => Response.json(data, { status });
const profiles = [1, 2, 3].map(id => ({ id, name: `角色${id}` }));

function fixture(t, fetch) {
  state.sessions = profiles.map(profile => ({ id: `voice_${profile.id}`, title: profile.name,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: profile.id }, messages: [] }));
  state.activeId = state.sessions[0].id;
  const app = setup(t, { fetch, callbacks: { state, getActive, getVoiceProfiles,
    getSession: id => state.sessions.find(session => session.id === id) } }).app;
  initVoiceSettings({});
  return { app, sessions: state.sessions };
}

test('voice switch ordering restores A after an unfinished home preview of B', { timeout: 3000 }, async t => {
  let serverActive = 1, finishB;
  const switches = [];
  const h = fixture(t, async (url, options = {}) => {
    if (url === '/api/voice/profiles') return response({ profiles, active_profile_id: serverActive });
    if (url === '/api/voice/switch') {
      const id = JSON.parse(options.body).profile_id; switches.push(id);
      if (id === 2) return new Promise(resolve => { finishB = () => {
        serverActive = id; resolve(response({ profile: '角色2', profile_id: id }));
      }; });
      serverActive = id; return response({ profile: `角色${id}`, profile_id: id });
    }
    return response({ backgrounds: [] });
  });
  await fetchVoiceProfiles();
  await h.app.actions.previewSession('voice_2'); await tick();
  assert.equal(typeof finishB, 'function');
  await h.app.actions.loadSession('voice_1'); await tick();
  finishB(); for (let i = 0; i < 5; i++) await tick();
  assert.deepEqual(switches, [2, 1]);
  assert.equal(serverActive, 1);
  assert.equal(getActive().settings.voiceProfileId, 1);
  assert.equal(getActiveProfileId(), 1);
});

test('voice switch ordering serializes writes and drops superseded queued choices', { timeout: 3000 }, async t => {
  let serverActive = 3, finishFirst, inFlight = 0, maximum = 0;
  const switches = [];
  const h = fixture(t, async (url, options = {}) => {
    if (url === '/api/voice/profiles') return response({ profiles, active_profile_id: serverActive });
    if (url === '/api/voice/switch') {
      const id = JSON.parse(options.body).profile_id;
      switches.push(id); maximum = Math.max(maximum, ++inFlight);
      if (switches.length === 1) return new Promise(resolve => { finishFirst = () => {
        serverActive = id; inFlight--; resolve(response({ profile_id: id }));
      }; });
      serverActive = id; inFlight--; return response({ profile_id: id });
    }
    return response({ backgrounds: [] });
  });
  await fetchVoiceProfiles();
  const first = ensureSessionVoice(h.sessions[0], { silent: true }); await tick();
  state.activeId = h.sessions[1].id;
  const second = ensureSessionVoice(h.sessions[1], { silent: true });
  state.activeId = h.sessions[2].id;
  const third = ensureSessionVoice(h.sessions[2], { silent: true }); await tick();
  const beforeRelease = [...switches];
  finishFirst(); await Promise.all([first, second, third]);
  assert.deepEqual(beforeRelease, [1]);
  assert.deepEqual(switches, [1, 3]);
  assert.equal(maximum, 1);
  assert.equal(serverActive, 3);
  assert.equal(getActiveProfileId(), 3);
});

test('voice switch ordering retries home entry after a failed load of the cached profile', { timeout: 3000 }, async t => {
  let engineReady = false;
  const switches = [];
  const h = fixture(t, async (url, options = {}) => {
    if (url === '/api/voice/profiles') return response({ profiles, active_profile_id: 1 });
    if (url === '/api/voice/switch') {
      switches.push(JSON.parse(options.body).profile_id);
      return engineReady ? response({ profile_id: 1 }) : response({ detail: '语音模型尚未加载' }, 503);
    }
    return response({ backgrounds: [] });
  });
  await fetchVoiceProfiles();
  await ensureSessionVoice(h.sessions[0], { silent: true });
  engineReady = true;
  await h.app.actions.loadSession('voice_1'); for (let i = 0; i < 5; i++) await tick();
  assert.deepEqual(switches, [1, 1]);
  assert.equal(getActiveProfileId(), 1);
});

test('voice switch ordering rejects a late active-profile snapshot but keeps its catalog', async t => {
  let active = 1, releaseList, holdList = false;
  const h = fixture(t, async (url, options = {}) => {
    if (url === '/api/voice/profiles') {
      const snapshot = { profiles: [...profiles, { id: 4, name: '新音色' }], active_profile_id: active };
      if (holdList) return new Promise(resolve => { releaseList = () => resolve(response(snapshot)); });
      return response(snapshot);
    }
    if (url === '/api/voice/switch') {
      active = JSON.parse(options.body).profile_id; return response({ profile_id: active });
    }
    return response({ backgrounds: [] });
  });
  await fetchVoiceProfiles();
  holdList = true;
  const listing = fetchVoiceProfiles();
  state.activeId = h.sessions[1].id;
  await ensureSessionVoice(h.sessions[1], { silent: true });
  releaseList(); await listing;
  assert.equal(getActiveProfileId(), 2);
  assert.ok(getVoiceProfiles().some(profile => profile.id === 4));
});

test('voice switch ordering can select a bound profile after one transient list failure', async t => {
  let listCalls = 0, switches = 0;
  const h = fixture(t, async (url, options = {}) => {
    if (url === '/api/voice/profiles') {
      listCalls++; if (listCalls === 1) return response({ profiles, active_profile_id: null });
      throw new TypeError('Failed to fetch');
    }
    if (url === '/api/voice/switch') { switches++; return response({ profile_id: JSON.parse(options.body).profile_id }); }
    return response({ backgrounds: [] });
  });
  await fetchVoiceProfiles(); listCalls = 0;
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/voice/profiles') { listCalls++; throw new TypeError('Failed to fetch'); }
    if (url === '/api/voice/switch') { switches++; return response({ profile_id: JSON.parse(options.body).profile_id }); }
    return response({ backgrounds: [] });
  };
  await ensureSessionVoice(h.sessions[0], { silent: true });
  assert.equal(listCalls, 1);
  assert.equal(switches, 1);
});

test('voice switch ordering continues with the latest choice after an earlier failure', async t => {
  let finishFirst;
  const switches = [];
  const h = fixture(t, async (url, options = {}) => {
    if (url === '/api/voice/profiles') return response({ profiles, active_profile_id: 3 });
    if (url === '/api/voice/switch') {
      const id = JSON.parse(options.body).profile_id; switches.push(id);
      if (id === 1) return new Promise(resolve => { finishFirst = () => resolve(response({ detail: '引擎暂时离线' }, 503)); });
      return response({ profile_id: id });
    }
    return response({ backgrounds: [] });
  });
  await fetchVoiceProfiles();
  const first = ensureSessionVoice(h.sessions[0], { silent: true }); await tick();
  state.activeId = h.sessions[1].id;
  const second = ensureSessionVoice(h.sessions[1], { silent: true }); await tick();
  const beforeRelease = [...switches]; finishFirst();
  await Promise.all([first, second]);
  assert.deepEqual(beforeRelease, [1]);
  assert.deepEqual(switches, [1, 2]);
  assert.equal(getActiveProfileId(), 2);
});

test('voice switch ordering drops a queued request when its binding is removed', async t => {
  let finishFirst;
  const switches = [];
  const h = fixture(t, async (url, options = {}) => {
    if (url === '/api/voice/profiles') return response({ profiles, active_profile_id: 3 });
    if (url === '/api/voice/switch') {
      const id = JSON.parse(options.body).profile_id; switches.push(id);
      if (id === 1) return new Promise(resolve => { finishFirst = () => resolve(response({ profile_id: id })); });
      return response({ profile_id: id });
    }
    return response({ backgrounds: [] });
  });
  await fetchVoiceProfiles();
  const first = ensureSessionVoice(h.sessions[0], { silent: true }); await tick();
  state.activeId = h.sessions[1].id;
  const second = ensureSessionVoice(h.sessions[1], { silent: true });
  h.sessions[1].settings.voiceProfileId = null;
  finishFirst(); await Promise.all([first, second]);
  assert.deepEqual(switches, [1]);
  assert.equal(h.sessions[1].settings.voiceProfileId, null);
});

test('voice switch ordering advances after a timed-out switch and ignores its late response', async t => {
  const setTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (callback, ms, ...args) => setTimeout(callback, ms === 120000 ? 5 : ms, ...args);
  t.after(() => { globalThis.setTimeout = setTimeout; });
  let finishLate, firstSignal;
  const switches = [];
  const h = fixture(t, async (url, options = {}) => {
    if (url === '/api/voice/profiles') return response({ profiles, active_profile_id: 3 });
    if (url === '/api/voice/switch') {
      const id = JSON.parse(options.body).profile_id; switches.push(id);
      if (id === 1) { firstSignal = options.signal; return new Promise(resolve => { finishLate = () => resolve(response({ profile_id: id })); }); }
      return response({ profile_id: id });
    }
    return response({ backgrounds: [] });
  });
  await fetchVoiceProfiles();
  const first = ensureSessionVoice(h.sessions[0], { silent: true }); await tick();
  state.activeId = h.sessions[1].id;
  const second = ensureSessionVoice(h.sessions[1], { silent: true });
  await Promise.all([first, second]);
  assert.deepEqual(switches, [1, 2]);
  assert.equal(firstSignal.aborted, true);
  finishLate(); await tick();
  assert.equal(getActiveProfileId(), 2);
});

test('unconfirmed default persistence invalidates cached selection and older reads without clearing the session binding', async t => {
  let finishSwitch, finishList, holdList = false, fail = true;
  let switches = 0;
  const h = fixture(t, async (url, options = {}) => {
    if (url === '/api/voice/profiles') {
      const snapshot = { profiles, active_profile_id: 1 };
      return holdList ? new Promise(resolve => { finishList = () => resolve(response(snapshot)); }) : response(snapshot);
    }
    if (url === '/api/voice/switch') {
      switches++;
      return fail ? new Promise(resolve => { finishSwitch = () => resolve(response({ detail: {
        error_code: 'VOICE_PROFILE_SAVE_FAILED', message: '启用音色尚未确认保存，请重新选择重试。', voice_profile_id: 1,
      } }, 500)); }) : response({ profile_id: 1 });
    }
    return response({ backgrounds: [] });
  });
  initVoiceSettings({}, { isHomeOpen: () => true });
  await fetchVoiceProfiles();
  const operation = ensureSessionVoice(h.sessions[0]); await tick();
  holdList = true;
  const listing = fetchVoiceProfiles();
  finishSwitch(); await operation;
  const afterFailure = getActiveProfileId();
  finishList(); await listing;
  assert.equal(afterFailure, null);
  assert.equal(h.sessions[0].settings.voiceProfileId, 1);
  assert.equal(switches, 1);
  assert.equal(getActiveProfileId(), null);
  holdList = false; fail = false;
  await ensureSessionVoice(h.sessions[0]);
  assert.equal(getActiveProfileId(), 1);
  assert.equal(switches, 2);
});

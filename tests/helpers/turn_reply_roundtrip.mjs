import fs from 'node:fs';
import { fakeDocument, Element } from './adv_dom.mjs';
import { initApp } from '../../frontend/src/main.js';
import { state as storeState } from '../../frontend/src/store.js';

const log = (msg) => { try { fs.writeSync(2, msg + '\n'); } catch (_) {} };

const port = process.argv[2] || '8080';
const targetSessionId = process.argv[3] || 'turn_roundtrip_test';
const baseUrl = `http://127.0.0.1:${port}`;

const doc = fakeDocument();
doc.querySelectorAll = s => doc.body.querySelectorAll(s);
doc.createDocumentFragment = () => {
  const frag = doc.createElement('fragment');
  frag.nodeType = 11;
  return frag;
};

const origAppendChild = Element.prototype.appendChild;
Element.prototype.appendChild = function (child) {
  if (child?.nodeType === 11) {
    const children = [...child.children];
    for (const c of children) this.appendChild(c);
    return child;
  }
  return origAppendChild.call(this, child);
};

globalThis.document = doc;
const nativeFetch = globalThis.fetch;
let observedRepairRequestBody = null;
let observedRepairResponseBody = null;
let releaseCleanupCalled = false;
let oldNullPostBranchTaken = false;
let repairPostCount = 0;
const observedTurnReplyEvents = [];
const historyCalls = [];
const eventsOrder = [];
const audioFetched = [];

const customFetch = async (input, init = {}) => {
  let url = typeof input === 'string' ? input : input?.url || '';
  if (url.startsWith('/')) {
    url = baseUrl + url;
  }
  log(`[fetch] ${init.method || 'GET'} ${url}`);

  // Non-core endpoints return small fakes
  if (url.includes('/api/characters') || url.includes('/api/backgrounds') ||
      url.includes('/api/system/version') || url.includes('/api/devices') ||
      url.includes('/api/config')) {
    return Response.json({ characters: [], backgrounds: [], devices: [] });
  }

  // Intercept turn-reply
  if (url.includes('/api/chat/turn-reply')) {
    const response = await nativeFetch(url, init);
    try {
      const data = await response.clone().json();
      observedTurnReplyEvents.push(data);
      log(`[fetch] turn-reply response: status=${data.status}, session=${data.session_id}, user_msg=${data.user_message_id}, msg_id=${data.message_id}`);
      if (data.status === 'pending' && !releaseCleanupCalled) {
        eventsOrder.push('pending');
        releaseCleanupCalled = true;
        log('[driver] observed real pending status! triggering /__test/release-cleanup');
        await nativeFetch(`${baseUrl}/__test/release-cleanup`, { method: 'POST' });
        eventsOrder.push('release');
      } else if (data.status === 'ready') {
        eventsOrder.push('ready');
      }
    } catch (e) {
      log(`[fetch] turn-reply parse error: ${e.message}`);
    }
    return response;
  }

  // Intercept regenerate-line
  if (url.includes('/api/voice/regenerate-line')) {
    repairPostCount++;
    eventsOrder.push('repair_post');
    const bodyText = typeof init.body === 'string' ? init.body : '';
    try {
      observedRepairRequestBody = JSON.parse(bodyText);
    } catch (_) {}

    // Old implementation sends message_id: null (kept for historical red only; candidate must not enter this branch)
    if (observedRepairRequestBody?.message_id === null && !releaseCleanupCalled) {
      oldNullPostBranchTaken = true;
      releaseCleanupCalled = true;
      try {
        await nativeFetch(`${baseUrl}/__test/release-cleanup`, { method: 'POST' });
      } catch (err) {
        console.error('Failed to trigger /__test/release-cleanup:', err);
      }
    }

    const response = await nativeFetch(url, init);
    try {
      observedRepairResponseBody = await response.clone().json();
      if (observedRepairResponseBody?.persisted) {
        eventsOrder.push('persisted_true');
      }
    } catch (_) {}
    return response;
  }

  if (url.includes('/api/chat/history')) {
    historyCalls.push(url);
  }

  if (url.includes('/audio/')) {
    audioFetched.push(url);
  }

  return nativeFetch(url, init);
};

class FakeImage extends Element {
  constructor() { super('img', doc); }
  decode() { return Promise.resolve(); }
}
globalThis.Image = FakeImage;

const storageMap = new Map();
globalThis.localStorage = {
  getItem: (key) => storageMap.has(key) ? storageMap.get(key) : null,
  setItem: (key, val) => { storageMap.set(key, String(val)); },
  removeItem: (key) => { storageMap.delete(key); },
  clear: () => { storageMap.clear(); },
};
globalThis.sessionStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};

let currentFetch = customFetch;

globalThis.window = {
  innerWidth: 1280,
  location: { origin: baseUrl, search: '', hash: '' },
  sessionStorage: globalThis.sessionStorage,
  get fetch() { return currentFetch; },
  set fetch(fn) { currentFetch = fn; },
};
globalThis.location = globalThis.window.location;
doc.defaultView = globalThis.window;

Object.defineProperty(globalThis, 'fetch', {
  get: () => currentFetch,
  set: (fn) => { currentFetch = fn; },
  configurable: true,
});

const audioController = {
  isPlaying: false,
  played: [],
  async playLine(index, chunks) {
    this.played.push({ index, chunks });
    for (const chunk of chunks || []) {
      if (chunk.audio_url) {
        try {
          await globalThis.fetch(chunk.audio_url);
        } catch (_) {}
      }
    }
    return 'ended';
  },
  stopLine() {},
  interrupt() {},
};

function createDom() {
  const advRoot = doc.createElement('div');
  advRoot.id = 'advRoot';
  doc.body.appendChild(advRoot);
  return { advRoot };
}

async function run() {
  const dom = createDom();

  const appOptions = {
    dom,
    initialize: true,
    renderSessionList: () => {},
    showTitle: () => {},
    showToast: () => {},
    streamAudioController: audioController,
    getVoiceProfiles: () => [{ id: 1, name: '测试音色' }],
    getCurrentActiveProvider: () => null,
    haveProvidersLoaded: () => true,
    hasProviderReadError: () => false,
  };

  log('[driver] step 1: starting app1');
  const app1 = initApp(appOptions);
  log('[driver] step 1: waiting app1.ready');
  await app1.ready;
  log('[driver] step 1: app1 ready');

  const session = storeState.sessions.find(s => s.id === targetSessionId);
  if (!session) {
    throw new Error(`Target session ${targetSessionId} not loaded from backend`);
  }
  storeState.activeId = targetSessionId;
  const initialLoadedCount = session.messages ? session.messages.length : 0;
  log(`[driver] step 1: loaded session count: ${initialLoadedCount}`);

  // 2. Send question through real application interface
  log('[driver] step 2: setting up line listener and sending message');
  let linePromiseResolve, linePromiseReject;
  const linePromise = new Promise((resolve, reject) => {
    linePromiseResolve = resolve;
    linePromiseReject = reject;
  });

  const lineTimer = setTimeout(() => {
    linePromiseReject(new Error('Timeout 8s waiting for line event from SSE'));
  }, 8000);

  const offLine = app1.conversation.on('line', line => {
    log('[driver] step 2: line event received!');
    clearTimeout(lineTimer);
    if (linePromiseResolve) {
      linePromiseResolve(line);
      linePromiseResolve = null;
    }
  });

  app1.sendMessage('测试提问');
  log('[driver] step 2: message sent, awaiting line');
  await linePromise;
  log('[driver] step 2: line promise resolved, calling stop');
  offLine?.();

  // Trigger real stop entry
  eventsOrder.push('stop');
  app1.conversation.stop();

  // Allow cancelled status to settle
  await new Promise(r => setTimeout(r, 200));

  // 3. Open LOG and click the repair button
  log('[driver] step 3: opening LOG backlog');
  app1.adv.action('log');
  await new Promise(r => setTimeout(r, 100));

  const backlogEl = app1.adv.backlog?.element;
  if (!backlogEl) {
    throw new Error('Backlog element missing');
  }

  const entries = backlogEl.querySelectorAll('.adv-log-entry');
  const targetEntry = [...entries].reverse().find(el => {
    const btn = el.querySelector('.adv-log-regenerate-btn');
    return btn && !btn.hidden;
  });
  const regenBtn = targetEntry?.querySelector('.adv-log-regenerate-btn') || backlogEl.querySelector('.adv-log-regenerate-btn');
  if (!regenBtn) {
    throw new Error('Regenerate button not found in backlog');
  }

  log('[driver] step 3: clicking regenerate button');
  regenBtn.click();

  // Wait for regenerate POST response
  log('[driver] step 3: waiting for regenerate response');
  const startWait = Date.now();
  while (!observedRepairResponseBody && Date.now() - startWait < 15000) {
    await new Promise(r => setTimeout(r, 50));
  }

  if (!observedRepairResponseBody) {
    throw new Error('Timeout waiting for regenerate-line response');
  }
  log(`[driver] step 3: regenerate response received: persisted=${observedRepairResponseBody.persisted}`);

  // Allow saveState to flush to localStorage
  await new Promise(r => setTimeout(r, 200));

  // 4. Dispose app1
  log('[driver] step 4: disposing app1');
  app1.conversation.stop();
  app1.adv?.unmount();

  // 5. Re-run initApp with preserved localStorage
  log('[driver] step 5: re-running initApp (app2) with preserved localStorage');
  const app2 = initApp(appOptions);
  await app2.ready;
  log('[driver] step 5: app2 ready');

  const reloadedSession = storeState.sessions.find(s => s.id === targetSessionId);
  const lastAssistant = reloadedSession?.messages?.findLast
    ? reloadedSession.messages.findLast(m => m.role === 'assistant')
    : [...(reloadedSession?.messages || [])].reverse().find(m => m.role === 'assistant');
  const reloadedLineAudio = lastAssistant?.lines?.[0]?.audio;
  const reloadedAudioUrl = reloadedLineAudio?.chunks?.[0]?.audio_url || null;
  const reloadedServerMessageId = lastAssistant?.serverMessageId ?? null;
  const diagnosticMsgId = typeof lastAssistant?.id === 'string' && /^m_(\d+)$/.test(lastAssistant.id)
    ? Number(/^m_(\d+)$/.exec(lastAssistant.id)[1])
    : null;

  let audioGetStatus = null;
  let audioIsRiffWave = false;
  if (reloadedAudioUrl) {
    const audioResp = await nativeFetch(`${baseUrl}${reloadedAudioUrl}`);
    audioGetStatus = audioResp.status;
    if (audioResp.status === 200) {
      const buf = Buffer.from(await audioResp.arrayBuffer());
      audioIsRiffWave = buf.subarray(0, 4).toString() === 'RIFF' && buf.subarray(8, 12).toString() === 'WAVE';
    }
  }

  const result = {
    initialLoadedCount,
    eventsOrder,
    turnReplyEvents: observedTurnReplyEvents,
    repairPostCount,
    oldNullPostBranchTaken,
    historyCalls,
    repairRequestMessageId: observedRepairRequestBody?.message_id ?? null,
    repairResponsePersisted: observedRepairResponseBody?.persisted ?? false,
    repairAudioUrl: observedRepairResponseBody?.audio?.chunks?.[0]?.audio_url ?? null,
    reloadedAudioUrl,
    reloadedServerMessageId,
    diagnosticMsgId,
    audioGetStatus,
    audioIsRiffWave,
    audioLostAfterReload: !reloadedAudioUrl || !reloadedAudioUrl.startsWith('/audio/repair_'),
    releaseCleanupCalled,
  };

  // Dispose app2
  app2.conversation.stop();
  app2.adv?.unmount();

  console.log('__ROUNDTRIP_RESULT__' + JSON.stringify(result));
}

run().catch(err => {
  console.error('ROUNDTRIP_FATAL:', err);
  process.exit(1);
});

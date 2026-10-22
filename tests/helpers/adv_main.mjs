import { initApp } from '../../frontend/src/main.js';
import { DEFAULT_SESSION_SETTINGS } from '../../frontend/src/store.js';
import { fakeDocument } from './adv_dom.mjs';
import { fakeChatStream } from './chat_stream.mjs';

export function setup(t, options = {}) {
  const doc = fakeDocument(); globalThis.document = doc;
  globalThis.window = { innerWidth: 1280 };
  t.after(() => { delete globalThis.document; });
  globalThis.localStorage = { getItem: () => null, setItem() {} };
  globalThis.fetch = options.fetch || (async () => Response.json({ characters: [] }));
  const sessions = [0, 1].map(n => ({ id: `s_${n}`, title: `Session ${n}`, messages: [],
    settings: { ...DEFAULT_SESSION_SETTINGS, advEnabled: n === 0, advTextIntervalMs: n === 0 ? 0 : 100 } }));
  const state = { sessions, activeId: sessions[0].id, global: { voiceMode: true } };
  const getActive = () => sessions.find(s => s.id === state.activeId);
  const dom = Object.fromEntries(['advRoot'].map(key => [key, doc.createElement('div')]));
  for (const node of Object.values(dom)) doc.body.appendChild(node);
  const audio = { isPlaying: false, played: [], sequential: [], currentMsgId: null,
    playLine(index, chunks) { this.played.push({ index, chunks }); return Promise.resolve('ended'); },
    stopLine() {}, startSession(...args) { this.sequential.push(args); }, enqueueChunk() {},
  };
  const noop = () => {};
  const stream = fakeChatStream(); let sequence = 0, deleting = false;
  const app = initApp({ initialize: false, dom, state, getActive,
    getSession: id => sessions.find(s => s.id === id), isSessionDeleting: () => deleting,
    getCurrentActiveProvider: () => null, getVoiceProfiles: () => [], haveProvidersLoaded: () => true,
    saveState: noop, autoTitle: noop, persistSession: async () => {}, clearMicTimers: noop,
    getCurrentVoice: () => null, stopCurrentVoice: noop, setCurrentVoice: noop,
    uid: () => `m_${++sequence}`, fetch: stream.fetch, showToast: noop, fetchAndCacheAudio: async () => {},
    streamAudioController: audio,
    addMessage(role, content) { const msg = { id: `m_${++sequence}`, role, content }; getActive().messages.push(msg); return msg; },
    fetchSessionHistoryPage: options.fetchPage || (async () => ({ messages: [], has_more: false })),
    ...options.callbacks,
  });
  app.fullRender(); t.after(() => { app.conversation.stop(); app.adv?.unmount(); });
  return { app, sessions, dom, state, audio, stream, getActive, setDeleting: value => { deleting = value; } };
}


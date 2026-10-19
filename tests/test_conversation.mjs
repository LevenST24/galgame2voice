import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createConversation } from '../frontend/src/conversation.js';
import { fakeChatStream, tick } from './helpers/chat_stream.mjs';

function setup({ voiceMode = false } = {}) {
  const session = { id: 's', title: '新对话', settings: { temperature: 0.7, voiceProfileId: 3 }, messages: [] };
  const state = { activeId: 's', global: { voiceMode, ttsPreset: 'balanced' } };
  const stream = fakeChatStream();
  const saves = [], persisted = [], cached = [], recordings = [], blocked = [], audio = [];
  let sequence = 0, currentVoice = null, control = null;
  const audioPlayer = {
    isPlaying: false, currentMsgId: null, paused: false,
    startSession(id, ctl, options) { this.currentMsgId = id; this.isPlaying = true; audio.push(['start', id, ctl, options]); },
    enqueueChunk(chunk) { audio.push(['chunk', chunk]); },
    finishInput() { audio.push(['finish']); },
    attachControl(ctl, id) { this.currentMsgId = id; audio.push(['attach', ctl, id]); },
    pause() { this.paused = true; }, resume() { this.paused = false; },
    interrupt(ms) { this.isPlaying = false; audio.push(['interrupt', ms]); },
  };
  const saveState = () => saves.push({ busy: conversation.busy, messages: structuredClone(session.messages) });
  const conversation = createConversation({
    state, getActive: () => session, getSession: id => id === session.id ? session : null,
    uid: prefix => `${prefix}_${++sequence}`, saveState,
    addMessage(role, content) {
      const message = { id: `m_${++sequence}`, role, content, ts: Date.now() };
      session.messages.push(message); session.title = content; saveState(); return message;
    },
    persistSession: async s => persisted.push(s.id), isSessionDeleting: () => false,
    audioStore: new Map(), putCachedAudioBlob: (...args) => recordings.push(args),
    fetchAndCacheAudio: async url => cached.push(url), audioPlayer,
    getCurrentVoice: () => currentVoice, setCurrentVoice: voice => { currentVoice = voice; },
    stopCurrentVoice: () => { currentVoice = null; audioPlayer.interrupt(40); },
    getAudioControl: () => control, onBlocked: reason => blocked.push(reason),
    fetch: stream.fetch,
  });
  return { conversation, session, state, stream, saves, persisted, cached, recordings, blocked, audio, audioPlayer,
    get voice() { return currentVoice; }, set control(value) { control = value; } };
}

test('normal turn emits in order, sends the same payload, and saves before unlocking', async () => {
  const app = setup();
  const events = [];
  for (const type of ['turnstart', 'text', 'audio_chunk', 'done', 'error', 'cancelled']) {
    app.conversation.on(type, payload => events.push([type, payload]));
  }
  app.conversation.send('  你好  ');
  assert.equal(app.conversation.busy, true);
  const request = app.stream.requests[0];
  assert.equal(request.url, '/api/chat/stream');
  assert.equal(request.options.method, 'POST');
  assert.deepEqual(request.options.headers, { 'Content-Type': 'application/json' });
  assert.deepEqual(JSON.parse(request.options.body), {
    prompt: '你好', session_id: 's', stream: true, voice_profile_id: 3,
    temperature: 0.7, preset: 'balanced',
  });
  await request.event('text', { delta_chinese: '你好' });
  await request.event('text', { delta_chinese: '！' });
  const chunk = { audio_url: '/audio/one.wav', sentence: 'こんにちは', emotion: 'happy', future_field: [1] };
  await request.event('audio_chunk', chunk);
  await request.onEnd('你好！', { tts_params: { speed: 1.1 } });
  assert.deepEqual(events.map(([type]) => type), ['turnstart', 'text', 'text', 'audio_chunk', 'done']);
  assert.equal(events[0][1].userMessage, app.session.messages[0]);
  assert.equal(events[0][1].assistantMessage.role, 'assistant');
  assert.deepEqual(events[1][1], { delta: '你好', text: '你好' });
  assert.deepEqual(events[2][1], { delta: '！', text: '你好！' });
  assert.deepEqual(events[3][1], chunk, 'audio events keep all server fields');
  const final = events.at(-1)[1].message;
  assert.equal(final, app.session.messages.at(-1));
  assert.equal(final.content, '你好！');
  assert.equal(final.japanese, 'こんにちは');
  assert.deepEqual(final.audioUrls, ['/audio/one.wav']);
  assert.deepEqual(final.audioChunks, [{ url: '/audio/one.wav', sentence: 'こんにちは', emotion: 'happy' }]);
  assert.deepEqual(final.ttsParams, { speed: 1.1 });
  assert.deepEqual(app.cached, ['/audio/one.wav']);
  assert.deepEqual(app.persisted, ['s']);
  assert.equal(app.saves.length, 2);
  assert.equal(app.saves.at(-1).busy, true);
  assert.equal(app.saves.at(-1).messages.length, 2);
  assert.equal(app.conversation.busy, false);
});

test('unknown server event types pass through unchanged and listeners unsubscribe', async () => {
  const app = setup();
  const received = [];
  const off = app.conversation.on('custom_event', payload => received.push(payload));
  app.conversation.send('下一句');
  const request = app.stream.requests[0];
  const custom = { id: 4, speaker: 'alice', text: '下一句', metadata: { pause: 0.5 } };
  await request.event('custom_event', custom);
  off();
  await request.event('custom_event', { id: 5 });
  assert.deepEqual(received, [custom]);
  await request.onEnd('');
});

test('busy ignores a second send without stopping audio or adding messages', async () => {
  const app = setup({ voiceMode: true });
  app.conversation.send('第一条');
  const request = app.stream.requests[0];
  await request.event('audio_chunk', { audio_url: '/audio/one.wav' });
  const previousAudio = [...app.audio];
  app.conversation.send('第二条');
  assert.equal(app.stream.requests.length, 1);
  assert.equal(app.session.messages.length, 1);
  assert.deepEqual(app.audio, previousAudio);
  await request.onEnd('');
  app.conversation.send('第二条');
  assert.equal(app.stream.requests.length, 2);
  app.conversation.stop();
});

test('stop aborts the stream, preserves partial text and emits cancelled once', async () => {
  const app = setup();
  const cancelled = [], done = [];
  app.conversation.on('cancelled', event => cancelled.push(event));
  app.conversation.on('done', event => done.push(event));
  app.conversation.send('等一下');
  const request = app.stream.requests[0];
  await request.onChunk('部分回复');
  app.conversation.stop();
  app.conversation.stop();
  await tick();
  assert.equal(request.options.signal.aborted, true);
  assert.equal(app.conversation.busy, false);
  assert.equal(cancelled.length, 1);
  assert.equal(cancelled[0].message.content, '部分回复');
  assert.equal(app.session.messages.at(-1), cancelled[0].message);
  assert.equal(app.session.messages[0].failed, undefined);
  assert.equal(done.length, 0);
});

test('stop from a listener prevents later events in the same network chunk', async () => {
  const app = setup();
  const received = [];
  app.conversation.on('text', () => app.conversation.stop());
  app.conversation.on('line_audio', event => received.push(event));
  app.conversation.send('停止');
  // A listener aborts synchronously while streamChat is parsing this packet.
  await app.stream.requests[0].packet([
    ['text', { delta_chinese: '收到' }],
    ['line_audio', { audio_url: '/audio/must-not-play.wav' }],
    ['done', { chinese: '收到更多内容' }],
  ]);
  assert.equal(app.conversation.busy, false);
  assert.deepEqual(received, []);
  assert.equal(app.session.messages.at(-1).content, '收到');
});

test('a turnstart listener can stop before fetch begins', () => {
  const app = setup();
  const cancelled = [];
  app.conversation.on('turnstart', () => app.conversation.stop());
  app.conversation.on('cancelled', event => cancelled.push(event));
  app.conversation.send('先别发送');
  assert.equal(app.conversation.busy, false);
  assert.equal(app.stream.requests.length, 0);
  assert.equal(cancelled.length, 1);
  assert.equal(cancelled[0].message, null);
  assert.equal(app.session.messages[0].failed, undefined);
});

test('a server error emits its kind and persists the inline failure on the user message', async () => {
  const app = setup();
  const errors = [];
  app.conversation.on('error', event => errors.push(event));
  app.conversation.send('提问');
  await app.stream.requests[0].onEnd('', { error: '尚未填写 API Key', errorCode: 'API_KEY_MISSING' });
  assert.equal(errors.length, 1);
  assert.equal(errors[0].error, '尚未填写 API Key');
  assert.equal(errors[0].kind, 'API_KEY_MISSING');
  assert.equal(errors[0].failedInline, true);
  assert.equal(errors[0].message, null);
  assert.deepEqual(app.session.messages[0].failed, { error: '尚未填写 API Key', code: 'API_KEY_MISSING' });
  assert.deepEqual(app.saves.at(-1).messages[0].failed, app.session.messages[0].failed);
  assert.equal(app.conversation.busy, false);
});

test('retry resends only the last failed message, keeping its position and recording', async () => {
  const app = setup();
  const blob = new Blob(['recording']);
  app.conversation.send('保留录音', { url: 'blob:recording', dur: 3, blob });
  await app.stream.requests[0].onEnd('', { error: '请求过于频繁', errorCode: 'RATE_LIMIT' });
  const user = app.session.messages[0];
  const id = user.id;
  app.conversation.retry(user);
  assert.equal(user.failed, undefined);
  assert.equal(app.session.messages.length, 1);
  assert.equal(user.id, id);
  assert.equal(user.dur, 3);
  assert.equal(user.voiceKey, `user_${id}`);
  assert.deepEqual(app.recordings, [[`user_${id}`, blob]]);
  assert.equal(app.stream.requests.length, 2);
  assert.equal(app.stream.requests[1].prompt, '保留录音');
  await app.stream.requests[1].onChunk('恢复了');
  await app.stream.requests[1].onEnd('恢复了');
  assert.equal(app.session.messages[0], user);
  assert.equal(app.session.messages[1].content, '恢复了');
  app.conversation.retry(user);
  assert.equal(app.stream.requests.length, 2);
});

test('continuous audio starts once, follows controls and attaches to the final message', async () => {
  const app = setup({ voiceMode: true });
  const control = { setPlaying() {}, setProgress() {} };
  app.control = control;
  app.conversation.on('done', ({ message }) => app.conversation.attachVoiceControl(control, message));
  app.conversation.send('语音');
  const request = app.stream.requests[0];
  await request.onChunk('说话');
  await request.event('audio_chunk', { audio_url: '/audio/one.wav', sentence: '一', emotion: 'happy' });
  await request.event('audio_chunk', { audio_url: '/audio/two.wav', sentence: '二' });
  app.voice.pause(); assert.equal(app.audioPlayer.paused, true);
  app.voice.resume(); assert.equal(app.audioPlayer.paused, false);
  await request.onEnd('说话');
  assert.deepEqual(app.audio.map(([type]) => type), ['start', 'chunk', 'chunk', 'finish', 'attach']);
  assert.equal(app.audio[0][2], control);
  assert.deepEqual(app.audio[1][1], { url: '/audio/one.wav', index: 0, sentence: '一', ctl: control, emotion: 'happy' });
  assert.equal(app.audio[2][1].index, 1);
  assert.equal(app.voice.msgId, app.session.messages.at(-1).id);
  assert.deepEqual(app.cached, []);
});

test('audio arriving before text can use the control created for later chunks', async () => {
  const app = setup({ voiceMode: true });
  app.conversation.send('先收到音频');
  const request = app.stream.requests[0];
  await request.event('audio_chunk', { audio_url: '/audio/early.wav' });
  assert.equal(app.audio[0][2], null);
  const control = { setPlaying() {}, setProgress() {} };
  app.control = control;
  await request.onChunk('后来收到文字');
  await request.event('audio_chunk', { audio_url: '/audio/later.wav' });
  assert.equal(app.audio[2][1].ctl, control);
  assert.equal(app.audio.filter(([type]) => type === 'start').length, 1);
  await request.onEnd('后来收到文字');
});

test('the final server correction replaces display text and saved history', async () => {
  const app = setup();
  const text = [];
  app.conversation.on('text', event => text.push(event));
  app.conversation.send('修正');
  const request = app.stream.requests[0];
  await request.onChunk('解析中的开场白');
  await request.onEnd('最终回复');
  assert.equal(text.at(-1).text, '最终回复');
  assert.equal(app.session.messages.at(-1).content, '最终回复');
});

test('a partial error stays in history and a switched session receives no autoplay', async () => {
  const app = setup({ voiceMode: true });
  let failure;
  app.conversation.on('error', event => { failure = event; });
  app.conversation.send('断开');
  const request = app.stream.requests[0];
  await request.onChunk('半句');
  app.state.activeId = 'other';
  await request.event('audio_chunk', { audio_url: '/audio/later.wav' });
  await request.onEnd('半句', { error: '回复连接提前结束' });
  assert.equal(failure.failedInline, false);
  assert.equal(failure.message.content, '半句');
  assert.equal(app.session.messages[0].failed, undefined);
  assert.equal(app.session.messages.at(-1).content, '半句');
  assert.deepEqual(app.audio, []);
  assert.deepEqual(app.cached, ['/audio/later.wav']);
});

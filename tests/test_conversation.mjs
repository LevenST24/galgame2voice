import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createConversation } from '../frontend/src/conversation.js';
import { fakeChatStream, tick } from './helpers/chat_stream.mjs';

function presentationEvents(app) {
  const events = [];
  for (const type of ['line', 'line_audio', 'done', 'error', 'cancelled']) {
    app.conversation.on(type, event => events.push({ type, ...event }));
  }
  return events;
}

const finalLine = (index, status = 'ready') => ({ index, zh: `终稿第${index}句`, ja: `最終${index}。`,
  emotion: 'gentle', act: {}, audio: { status, chunks: status === 'ready' ? [{ index: 0, audio_url: `/audio/final${index}.wav` }] : [] } });

test('final reply: whole-reply format reaches the stage without changing stored line identity', async () => {
  const app = setup(); app.session.settings.advReplyFormat = 'legacy';
  const events = presentationEvents(app);
  app.conversation.send('旧格式');
  const request = app.stream.requests[0];
  assert.equal(request.reply_format, 'old');
  await request.onEnd('终稿中文', { japanese: '最終の日本語。', total_audio_url: '/audio/whole.wav' });
  assert.deepEqual(events.map(event => event.type), ['line', 'line_audio', 'done']);
  assert.equal(events[0].line.zh, '终稿中文');
  assert.equal(events[0].line.ja, '最終の日本語。');
  assert.equal(events[1].status, 'ready');
  assert.deepEqual(events[1].chunks.map(chunk => chunk.audio_url), ['/audio/whole.wav']);
  const stored = app.session.messages.at(-1);
  assert.equal(stored.lines, undefined, 'presentation line zero is not a server history line');
  assert.equal(events.at(-1).message, stored);
});

test('final reply: terminal-only structured lines are delivered in order before completion', async () => {
  const app = setup(); const events = presentationEvents(app);
  app.conversation.send('终稿给出台词');
  await app.stream.requests[0].onEnd('', { lines: [finalLine(1), finalLine(0)] });
  assert.deepEqual(events.filter(event => event.type === 'line').map(event => event.line.index), [0, 1]);
  assert.deepEqual(events.filter(event => event.type === 'line_audio').map(event => event.line), [0, 1]);
  assert.equal(events.at(-1).type, 'done');
  assert.equal(app.session.messages.at(-1).lines.length, 2);
});

test('final reply: completes missing lines without replaying an already announced line or audio', async () => {
  const app = setup(); const events = presentationEvents(app);
  app.conversation.send('部分事件已收到');
  const request = app.stream.requests[0];
  await request.event('line', finalLine(0));
  await request.event('line_audio', { line: 0, ...finalLine(0).audio });
  await request.onEnd('', { lines: [finalLine(0), finalLine(1)] });
  assert.deepEqual(events.filter(event => event.type === 'line').map(event => event.line.index), [0, 1]);
  assert.deepEqual(events.filter(event => event.type === 'line_audio').map(event => event.line), [0, 1]);
});

test('final reply: terminal audio releases a line whose earlier audio event was absent', async () => {
  const app = setup(); const events = presentationEvents(app);
  app.conversation.send('等待音频');
  const request = app.stream.requests[0];
  await request.event('line', finalLine(0));
  await request.onEnd('', { lines: [finalLine(0)] });
  assert.equal(events.filter(event => event.type === 'line').length, 1);
  assert.equal(events.filter(event => event.type === 'line_audio').length, 1);
  assert.equal(events.find(event => event.type === 'line_audio').status, 'ready');
});

test('final reply: a pending audio result at stream completion becomes an explicit failure', async () => {
  const app = setup(); const events = presentationEvents(app);
  app.conversation.send('没有音频');
  const request = app.stream.requests[0];
  await request.event('line', finalLine(0));
  await request.onEnd('', { lines: [finalLine(0, 'pending')] });
  assert.equal(events.find(event => event.type === 'line_audio')?.status, 'failed');
});

test('final reply: Japanese-only whole reply is saved and presented', async () => {
  const app = setup(); const events = presentationEvents(app);
  app.conversation.send('只有日文');
  await app.stream.requests[0].onEnd('', { japanese: '日本語だけの回答。' });
  assert.equal(app.session.messages.at(-1).role, 'assistant');
  assert.equal(app.session.messages.at(-1).japanese, '日本語だけの回答。');
  assert.equal(events.find(event => event.type === 'line')?.line.ja, '日本語だけの回答。');
});

test('final reply: presentation keeps duplicate audio URLs in their sentence positions', async () => {
  const app = setup(); const events = presentationEvents(app);
  app.conversation.send('重复句');
  const request = app.stream.requests[0];
  await request.event('audio_chunk', { audio_url: '/audio/same.wav', sentence: 'はい。' });
  await request.event('audio_chunk', { audio_url: '/audio/same.wav', sentence: 'はい。' });
  await request.onEnd('是的。是的。');
  const audio = events.find(event => event.type === 'line_audio');
  assert.deepEqual(audio?.chunks.map(chunk => chunk.audio_url), ['/audio/same.wav', '/audio/same.wav']);
  assert.deepEqual(audio.chunks.map(chunk => chunk.sentence), ['はい。', 'はい。']);
});

test('final reply: failure does not start playback of a partial legacy reply', async () => {
  const app = setup(); const events = presentationEvents(app);
  app.conversation.send('中断');
  const request = app.stream.requests[0];
  await request.onChunk('已收到的部分');
  await request.onEnd('', { error: '模拟连接中断' });
  assert.deepEqual(events.map(event => event.type), ['error']);
  assert.equal(app.session.messages.at(-1).content, '已收到的部分');
});

test('final reply: listeners cannot start another request before final presentation is settled', async () => {
  const app = setup();
  const attempts = [];
  app.conversation.on('line', () => attempts.push(app.conversation.send('不能抢先发出')));
  app.conversation.send('终稿');
  await app.stream.requests[0].onEnd('终稿内容');
  assert.deepEqual(attempts, [false]);
  assert.equal(app.stream.requests.length, 1);
  assert.equal(app.conversation.busy, false);
});

function setup({ voiceMode = false } = {}) {
  const session = { id: 's', title: '新对话', settings: { temperature: 0.7, voiceProfileId: 3 }, messages: [] };
  const state = { activeId: 's', global: { voiceMode, ttsPreset: 'balanced' } };
  const stream = fakeChatStream(); let deleting = false;
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
    persistSession: async s => persisted.push(s.id), isSessionDeleting: () => deleting,
    audioStore: new Map(), putCachedAudioBlob: (...args) => recordings.push(args),
    fetchAndCacheAudio: async url => cached.push(url), audioPlayer,
    getCurrentVoice: () => currentVoice, setCurrentVoice: voice => { currentVoice = voice; },
    stopCurrentVoice: () => { currentVoice = null; audioPlayer.interrupt(40); },
    getAudioControl: () => control, onBlocked: reason => blocked.push(reason),
    fetch: stream.fetch,
  });
  return { conversation, session, state, stream, saves, persisted, cached, recordings, blocked, audio, audioPlayer,
    get voice() { return currentVoice; }, set control(value) { control = value; }, setDeleting: value => { deleting = value; } };
}

test('send explicitly reports acceptance and busy includes loading/deleting', () => {
  const h = setup();
  assert.equal(h.conversation.send(' '), false);
  h.session.loadingOlder = true; assert.equal(h.conversation.busy, true); assert.equal(h.conversation.send('问题'), false);
  h.session.loadingOlder = false; h.setDeleting(true);
  assert.equal(h.conversation.busy, true); assert.equal(h.conversation.send('问题'), false);
  h.setDeleting(false); assert.equal(h.conversation.busy, false);
  assert.equal(h.conversation.send('问题'), true); assert.equal(h.conversation.send('忙碌'), false);
  h.conversation.stop();
});

test('one failing UI listener cannot block the request, other listeners or saving the reply', async t => {
  const h = setup();
  const errors = [], seen = [];
  t.mock.method(console, 'error', (...args) => errors.push(args));
  for (const type of ['turnstart', 'text', 'line', 'line_audio', 'done']) {
    h.conversation.on(type, () => { throw new Error(`synthetic ${type} failure`); });
    h.conversation.on(type, () => seen.push(type));
  }
  assert.equal(h.conversation.send('测试请求'), true);
  assert.equal(h.stream.requests.length, 1);
  const request = h.stream.requests[0];
  await request.onChunk('已收到');
  await request.event('line', finalLine(0));
  await request.event('line_audio', { line: 0, ...finalLine(0).audio });
  await request.onEnd('已收到', { lines: [finalLine(0)] });
  assert.deepEqual(seen, ['turnstart', 'text', 'line', 'line_audio', 'done']);
  assert.equal(errors.length, seen.length);
  assert.equal(h.conversation.busy, false);
  assert.equal(h.session.messages.at(-1).content, '已收到');
  assert.equal(h.stream.requests.length, 1, 'a UI error must not resend the request');
});

test('canRetry is pure and matches retry eligibility, including partial failure', async () => {
  const h = setup(); h.conversation.send('问题');
  await h.stream.requests[0].onEnd('', { error: '离线' });
  const user = h.session.messages[0], before = structuredClone(user);
  assert.equal(h.conversation.canRetry(user), true); assert.deepEqual(user, before);
  assert.equal(h.stream.requests.length, 1);
  h.session.loadingOlder = true; assert.equal(h.conversation.canRetry(user), false); h.conversation.retry(user);
  h.session.loadingOlder = false; h.setDeleting(true); assert.equal(h.conversation.canRetry(user), false); h.conversation.retry(user);
  h.setDeleting(false); assert.equal(h.stream.requests.length, 1);
  h.conversation.retry(user); assert.equal(h.stream.requests.length, 2); assert.equal(h.conversation.canRetry(user), false);
  await h.stream.requests[1].event('line', { index: 0, zh: '部分', ja: '', emotion: '', act: {} });
  await h.stream.requests[1].onEnd('', { error: '中断' });
  assert.equal(h.conversation.canRetry(user), false); h.conversation.retry(user);
  assert.equal(h.stream.requests.length, 2); assert.equal(h.session.messages.length, 2);
});

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
    temperature: 0.7, reply_format: 'lines', preset: 'balanced',
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

test('audio arrival only caches; ADV owns output, including early chunks', async () => {
  const app = setup({ voiceMode: true });
  app.conversation.send('语音'); const request = app.stream.requests[0];
  await request.event('audio_chunk', { audio_url: '/audio/early.wav' });
  await request.onChunk('回复');
  await request.event('audio_chunk', { audio_url: '/audio/later.wav' });
  await request.onEnd('回复');
  assert.deepEqual(app.audio, []);
  assert.deepEqual(app.cached, ['/audio/early.wav', '/audio/later.wav']);
  assert.equal(app.session.messages.at(-1).content, '回复');
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

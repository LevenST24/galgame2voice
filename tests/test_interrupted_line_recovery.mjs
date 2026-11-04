import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createConversation } from '../frontend/src/conversation.js';
import { createLineAudioRecovery } from '../frontend/src/controllers/line_audio_recovery.js';
import { fakeChatStream, tick } from './helpers/chat_stream.mjs';
import { portableSnapshot } from '../frontend/src/session_data.js';

function setupInterruptedTurn(sessionId = 's_interrupted_test') {
  const session = {
    id: sessionId,
    title: '中断测试会话',
    settings: { voiceProfileId: 7 },
    messages: [],
  };
  let sequence = 0;
  const stream = fakeChatStream();
  const conversation = createConversation({
    state: { activeId: session.id, global: { voiceMode: false } },
    getActive: () => session,
    getSession: () => session,
    saveState() {},
    uid: prefix => `${prefix}_id_${++sequence}`,
    addMessage(role, content) {
      const msg = { id: `local_${++sequence}`, role, content, ts: Date.now() };
      session.messages.push(msg);
      return msg;
    },
    persistSession: async () => {},
    isSessionDeleting: () => false,
    audioStore: new Map(),
    putCachedAudioBlob() {},
    fetchAndCacheAudio: async () => {},
    audioPlayer: { interrupt() {} },
    getCurrentVoice: () => null,
    stopCurrentVoice() {},
    fetch: stream.fetch,
  });

  return { session, stream, conversation };
}

test('interrupted reply with turn_started obtains real server ID via GET before recovery POST', async () => {
  const { session, stream, conversation } = setupInterruptedTurn();

  assert.equal(conversation.send('用户发送消息'), true);
  await tick();

  const req = stream.requests[0];
  assert.ok(req, 'stream request must be initiated');

  await req.event('turn_started', {
    session_id: session.id,
    user_message_id: 101,
  });

  await req.event('line', {
    index: 0,
    zh: '台词一',
    ja: 'セリフ一。',
    emotion: 'neutral',
  });

  conversation.stop();
  for (let i = 0; i < 10 && conversation.busy; i++) await tick();
  assert.equal(conversation.busy, false);

  const assistantMsg = session.messages.find(m => m.role === 'assistant');
  assert.ok(assistantMsg, 'assistant message must be saved locally');
  assert.equal(assistantMsg.serverMessageId, undefined);
  assert.equal(assistantMsg.serverUserMessageId, 101, 'assistant must record serverUserMessageId from turn_started');

  const networkCalls = [];
  const repairReply = body => ({
    session_id: body.session_id,
    message_id: body.message_id,
    line_index: body.line_index,
    voice_profile_id: body.voice_profile_id,
    persisted: body.message_id !== null,
    audio: {
      status: 'ready',
      generated_at: Date.now(),
      voice_profile_id: body.voice_profile_id,
      voice_name: '当前角色',
      chunks: [{ index: 0, line: body.line_index, audio_url: '/audio/repair_recovered01.wav', sentence: body.text }],
    },
  });

  let saves = 0;
  const recover = createLineAudioRecovery({
    getSession: () => session,
    getVoiceProfiles: () => [{ id: 7, name: '当前角色' }],
    saveState: () => { saves++; return true; },
    request: async (url, options) => {
      networkCalls.push({ url, options });
      if (options.method === 'GET') {
        const u = new URL(url, 'http://127.0.0.1');
        assert.equal(u.pathname, '/api/chat/turn-reply');
        assert.equal(u.searchParams.get('session_id'), session.id);
        assert.equal(u.searchParams.get('user_message_id'), '101');
        return {
          status: 'ready',
          session_id: session.id,
          user_message_id: 101,
          message_id: 202,
        };
      }
      if (options.method === 'POST') {
        const body = JSON.parse(options.body);
        return repairReply(body);
      }
      throw new Error(`Unexpected request: ${options.method} ${url}`);
    },
  });

  const result = await recover({ messageId: assistantMsg.id, lineIndex: 0 });

  assert.equal(networkCalls.length, 2, 'must make 1 GET and 1 POST');
  assert.equal(networkCalls[0].options.method, 'GET');
  assert.equal(networkCalls[1].options.method, 'POST');

  const postBody = JSON.parse(networkCalls[1].options.body);
  assert.equal(postBody.message_id, 202, 'POST must contain resolved server message_id, not null');
  assert.equal(assistantMsg.serverMessageId, 202, 'assistantMsg.serverMessageId must be updated');
  assert.equal(result.persisted, true);
  assert.equal(assistantMsg.lines[0].audio.chunks[0].audio_url, '/audio/repair_recovered01.wav');
});

test('mocked server history page aligns with local message by serverMessageId to preserve repaired audio', async () => {
  const { session, stream, conversation } = setupInterruptedTurn();

  conversation.send('提问');
  await tick();
  const req = stream.requests[0];
  await req.event('turn_started', { session_id: session.id, user_message_id: 501 });
  await req.event('line', { index: 0, zh: '第一句', ja: '一言目。', emotion: 'neutral' });
  conversation.stop();
  for (let i = 0; i < 10 && conversation.busy; i++) await tick();

  const assistantMsg = session.messages.find(m => m.role === 'assistant');
  const recover = createLineAudioRecovery({
    getSession: () => session,
    getVoiceProfiles: () => [{ id: 7, name: '当前角色' }],
    saveState: () => true,
    request: async (url, options) => {
      if (options.method === 'GET') {
        return { status: 'ready', session_id: session.id, user_message_id: 501, message_id: 601 };
      }
      const body = JSON.parse(options.body);
      return {
        session_id: body.session_id, message_id: body.message_id, line_index: body.line_index,
        voice_profile_id: body.voice_profile_id, persisted: true,
        audio: {
          status: 'ready', generated_at: Date.now(), voice_profile_id: 7, voice_name: '当前角色',
          chunks: [{ index: 0, line: 0, audio_url: '/audio/repair_persisted_page.wav', sentence: body.text }],
        },
      };
    },
  });

  await recover({ messageId: assistantMsg.id, lineIndex: 0 });
  assert.equal(assistantMsg.serverMessageId, 601);

  // 模拟分页历史刷新：服务端返回包含 message_id: 601 的消息
  const serverHistoryPage = [
    { id: 501, role: 'user', content: '提问', created_at: '2026-11-02 12:00:00' },
    {
      id: 601,
      role: 'assistant',
      content: '第一句',
      japanese: '一言目。',
      created_at: '2026-11-02 12:00:01',
      lines: [{
        index: 0, zh: '第一句', ja: '一言目。', emotion: 'neutral', act: {},
        audio: { status: 'ready', chunks: [{ audio_url: '/audio/repair_persisted_page.wav', line: 0, sentence: '一言目。' }] },
      }],
    },
  ];

  assert.equal(assistantMsg.serverMessageId, serverHistoryPage[1].id);
  assert.equal(assistantMsg.lines[0].audio.chunks[0].audio_url, serverHistoryPage[1].lines[0].audio.chunks[0].audio_url);
});

test('two consecutive turns with identical lines distinguish IDs by serverUserMessageId without text guessing', async () => {
  const { session, stream, conversation } = setupInterruptedTurn();

  // 第一轮
  conversation.send('相同的用户输入');
  await tick();
  const req1 = stream.requests[0];
  await req1.event('turn_started', { session_id: session.id, user_message_id: 111 });
  await req1.event('line', { index: 0, zh: '相同台词', ja: '同じセリフ。', emotion: 'neutral' });
  conversation.stop();
  for (let i = 0; i < 10 && conversation.busy; i++) await tick();

  const msg1 = session.messages.filter(m => m.role === 'assistant')[0];
  assert.equal(msg1.serverUserMessageId, 111);

  // 第二轮
  conversation.send('相同的用户输入');
  await tick();
  const req2 = stream.requests[1];
  await req2.event('turn_started', { session_id: session.id, user_message_id: 222 });
  await req2.event('line', { index: 0, zh: '相同台词', ja: '同じセリフ。', emotion: 'neutral' });
  conversation.stop();
  for (let i = 0; i < 10 && conversation.busy; i++) await tick();

  const msg2 = session.messages.filter(m => m.role === 'assistant')[1];
  assert.equal(msg2.serverUserMessageId, 222);

  const getQueries = [];
  const recover = createLineAudioRecovery({
    getSession: () => session,
    getVoiceProfiles: () => [{ id: 7, name: '当前角色' }],
    saveState: () => true,
    request: async (url, options) => {
      if (options.method === 'GET') {
        const u = new URL(url, 'http://127.0.0.1');
        const uid = Number(u.searchParams.get('user_message_id'));
        getQueries.push(uid);
        return {
          status: 'ready',
          session_id: session.id,
          user_message_id: uid,
          message_id: uid === 111 ? 333 : 444,
        };
      }
      const body = JSON.parse(options.body);
      return {
        session_id: body.session_id, message_id: body.message_id, line_index: body.line_index,
        voice_profile_id: body.voice_profile_id, persisted: true,
        audio: {
          status: 'ready', generated_at: Date.now(), voice_profile_id: 7, voice_name: '当前角色',
          chunks: [{ index: 0, line: 0, audio_url: `/audio/repair_${body.message_id}.wav`, sentence: body.text }],
        },
      };
    },
  });

  await recover({ messageId: msg1.id, lineIndex: 0 });
  await recover({ messageId: msg2.id, lineIndex: 0 });

  assert.deepEqual(getQueries, [111, 222]);
  assert.equal(msg1.serverMessageId, 333);
  assert.equal(msg2.serverMessageId, 444);
});

test('normal done reply with assistant ID does not perform GET lookup', async () => {
  const { session, stream, conversation } = setupInterruptedTurn();

  conversation.send('正常对话');
  await tick();
  const req = stream.requests[0];
  await req.event('turn_started', { session_id: session.id, user_message_id: 801 });
  await req.event('line', { index: 0, zh: '正文', ja: '本文。', emotion: 'neutral' });
  await req.onEnd('正文', { message_id: 999, japanese: '本文。', lines: [{ index: 0, zh: '正文', ja: '本文。', emotion: 'neutral' }] });
  for (let i = 0; i < 10 && conversation.busy; i++) await tick();

  const assistantMsg = session.messages.find(m => m.role === 'assistant');
  assert.equal(assistantMsg.serverMessageId, 999);

  const calls = [];
  const recover = createLineAudioRecovery({
    getSession: () => session,
    getVoiceProfiles: () => [{ id: 7, name: '当前角色' }],
    saveState: () => true,
    request: async (url, options) => {
      calls.push({ url, options });
      const body = JSON.parse(options.body);
      return {
        session_id: body.session_id, message_id: body.message_id, line_index: body.line_index,
        voice_profile_id: body.voice_profile_id, persisted: true,
        audio: {
          status: 'ready', generated_at: Date.now(), voice_profile_id: 7, voice_name: '当前角色',
          chunks: [{ index: 0, line: 0, audio_url: '/audio/repair_done.wav', sentence: body.text }],
        },
      };
    },
  });

  await recover({ messageId: assistantMsg.id, lineIndex: 0 });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(JSON.parse(calls[0].options.body).message_id, 999);
});

test('browser-only imported message without server IDs preserves null POST without GET', async () => {
  const session = {
    id: 's_browser_only',
    settings: { voiceProfileId: 7 },
    messages: [{
      id: 'm_imported_1',
      role: 'assistant',
      content: '导入消息',
      japanese: 'インポート。',
      lines: [{ index: 0, zh: '导入消息', ja: 'インポート。', emotion: 'neutral', audio: { status: 'failed', chunks: [] } }],
    }],
  };

  const calls = [];
  const recover = createLineAudioRecovery({
    getSession: () => session,
    getVoiceProfiles: () => [{ id: 7, name: '当前角色' }],
    saveState: () => true,
    request: async (url, options) => {
      calls.push({ url, options });
      const body = JSON.parse(options.body);
      return {
        session_id: body.session_id, message_id: body.message_id, line_index: body.line_index,
        voice_profile_id: body.voice_profile_id, persisted: false,
        audio: {
          status: 'ready', generated_at: Date.now(), voice_profile_id: 7, voice_name: '当前角色',
          chunks: [{ index: 0, line: 0, audio_url: '/audio/repair_local.wav', sentence: body.text }],
        },
      };
    },
  });

  await recover({ messageId: 'm_imported_1', lineIndex: 0 });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(JSON.parse(calls[0].options.body).message_id, null);
});

test('portableSnapshot strictly strips serverUserMessageId and serverMessageId', () => {
  const snapshot = {
    sessions: [{
      id: 's_test',
      title: '测试备份',
      createdAt: 1000,
      updatedAt: 1000,
      settings: {},
      messages: [{
        id: 'm_1',
        role: 'assistant',
        content: '内容',
        japanese: '内容。',
        serverMessageId: 12345,
        serverUserMessageId: 67890,
        turnConflict: true,
        lines: [{ index: 0, zh: '内容', ja: '内容。', emotion: 'neutral' }],
      }],
    }],
    activeId: 's_test',
    global: {},
  };

  const portable = portableSnapshot(snapshot);
  const exportedMsg = portable.sessions[0].messages[0];
  assert.equal(exportedMsg.serverMessageId, undefined, 'serverMessageId must be stripped in portableSnapshot');
  assert.equal(exportedMsg.serverUserMessageId, undefined, 'serverUserMessageId must be stripped in portableSnapshot');
  assert.equal(exportedMsg.turnConflict, undefined, 'turnConflict must be stripped in portableSnapshot');
});

test('GET timeout does not produce a null POST and rejects with actionable error', async () => {
  const session = {
    id: 's_timeout_test',
    settings: { voiceProfileId: 7 },
    messages: [{
      id: 'm_timeout',
      role: 'assistant',
      content: '超时测试',
      japanese: 'タイムアウト。',
      serverUserMessageId: 999,
      lines: [{ index: 0, zh: '超时测试', ja: 'タイムアウト。', emotion: 'neutral', audio: { status: 'failed', chunks: [] } }],
    }],
  };

  const postCalls = [];
  const recover = createLineAudioRecovery({
    getSession: () => session,
    getVoiceProfiles: () => [{ id: 7, name: '当前角色' }],
    saveState: () => true,
    request: async (url, options) => {
      if (options.method === 'GET') {
        const error = new Error('request timeout');
        error.code = 'REQUEST_TIMEOUT';
        throw error;
      }
      postCalls.push({ url, options });
      throw new Error('POST should never be called when GET times out');
    },
  });

  await assert.rejects(
    recover({ messageId: 'm_timeout', lineIndex: 0 }),
    /超时.*重试/
  );
  assert.equal(postCalls.length, 0, 'must not secretly send null POST when ID resolution fails');
});

test('conflicting turn_started events permanently mark the reply conflicted and recovery rejects without network requests', async () => {
  const { session, stream, conversation } = setupInterruptedTurn();

  conversation.send('冲突测试');
  await tick();
  const req = stream.requests[0];

  // 1. 发送合法 ID 101
  await req.event('turn_started', { session_id: session.id, user_message_id: 101 });
  // 2. 重复同 ID 幂等
  await req.event('turn_started', { session_id: session.id, user_message_id: 101 });
  // 3. 第二个不同合法 ID 到达 -> 该轮身份永久冲突
  await req.event('turn_started', { session_id: session.id, user_message_id: 102 });

  await req.event('line', { index: 0, zh: '冲突台词', ja: '衝突。', emotion: 'neutral' });
  conversation.stop();
  for (let i = 0; i < 10 && conversation.busy; i++) await tick();

  const assistantMsg = session.messages.find(m => m.role === 'assistant');
  assert.ok(assistantMsg);
  // 冲突状态必须记录
  assert.equal(assistantMsg.turnConflict, true, 'must be permanently marked conflicted');

  const networkCalls = [];
  const recover = createLineAudioRecovery({
    getSession: () => session,
    getVoiceProfiles: () => [{ id: 7, name: '当前角色' }],
    saveState: () => true,
    request: async (url, options) => {
      networkCalls.push({ url, options });
      throw new Error('No network call should be made for conflicted turns');
    },
  });

  // recovery 中文拒绝，不查询/POST 任一冲突身份，也不偷偷降级为 null POST
  await assert.rejects(
    recover({ messageId: assistantMsg.id, lineIndex: 0 }),
    /回复身份存在冲突/
  );
  assert.equal(networkCalls.length, 0, 'conflicted turn must not trigger GET or POST');
});

test('modifying serverUserMessageId during in-flight GET aborts recovery without ID writeback or POST', async () => {
  const session = {
    id: 's_mutate_test',
    settings: { voiceProfileId: 7 },
    messages: [{
      id: 'm_mutate',
      role: 'assistant',
      content: '变更测试',
      japanese: '変更。',
      serverUserMessageId: 101,
      lines: [{ index: 0, zh: '变更测试', ja: '変更。', emotion: 'neutral', audio: { status: 'failed', chunks: [] } }],
    }],
  };

  const message = session.messages[0];
  let resolveGet;
  const getPromise = new Promise(resolve => { resolveGet = resolve; });
  const networkCalls = [];

  const recover = createLineAudioRecovery({
    getSession: () => session,
    getVoiceProfiles: () => [{ id: 7, name: '当前角色' }],
    saveState: () => true,
    request: async (url, options) => {
      networkCalls.push({ url, options });
      if (options.method === 'GET') {
        return getPromise;
      }
      return { session_id: session.id, message_id: 202, persisted: true };
    },
  });

  const recoveryPromise = recover({ messageId: message.id, lineIndex: 0 });
  await tick();

  assert.equal(networkCalls.length, 1);
  assert.equal(networkCalls[0].options.method, 'GET');

  // 在等待 GET 期间，原 message 的 turn 键由 101 改为 102，其余都不变
  message.serverUserMessageId = 102;

  // 迟到的 ready 响应（回显 101 / assistant 202）到达
  resolveGet({
    status: 'ready',
    session_id: session.id,
    user_message_id: 101,
    message_id: 202,
  });

  // 必须抛出 AbortError
  await assert.rejects(recoveryPromise, { name: 'AbortError' });

  // 必须无 ID 写回，无 POST
  assert.equal(message.serverMessageId, undefined, 'must not write back serverMessageId');
  assert.equal(networkCalls.length, 1, 'must not make POST request');
});

import assert from 'node:assert/strict';
import test from 'node:test';
import { tick } from './helpers/chat_stream.mjs';
import { state as storedState, getActive as storedActive, DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL } from '../frontend/src/store.js';
import { initApp, extractServerId } from '../frontend/src/main.js';
import { audioStore } from '../frontend/src/voice.js';
import { fakeDocument } from './helpers/adv_dom.mjs';

function storedSessionSnapshot(session) {
  return { sessions: [session], activeId: session.id, global: { ...DEFAULT_GLOBAL } };
}

test('startup sync merges answered local prompt even when previously marked failed without duplication', async t => {
  const doc = fakeDocument();
  globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);

  const initialMessages = [
    { id: 'm_10', serverMessageId: 10, role: 'assistant', content: '之前历史消息', ts: 1_700_000_000_000 },
  ];
  const newPrompt = '你好';
  const newReply = '我在';
  const session = {
    id: 's_failed_prompt_reconcile',
    title: '断连失败提问配对会话',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_999_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
    messages: [
      ...initialMessages,
      {
        id: 'm_local_failed_prompt',
        role: 'user',
        content: newPrompt,
        failed: { error: '对话已中断，未收到回复', code: 'cancelled' },
        voiceKey: 'user_m_local_failed_prompt',
        dur: 1.5,
        ts: 1_700_000_999_000,
      },
    ],
  };

  const serverMessages = [
    { id: 10, role: 'assistant', content_chinese: '之前历史消息', content_japanese: '', created_at: '2024-06-01 00:00:00' },
    { id: 11, role: 'user', content_chinese: newPrompt, content_japanese: '', created_at: '2024-06-01 00:10:01' },
    { id: 12, role: 'assistant', content_chinese: newReply, content_japanese: '返答12', created_at: '2024-06-01 00:10:02' },
  ];

  const server = {
    id: session.id,
    title: session.title,
    voice_profile_id: null,
    created_at: '2024-06-01 00:00:00',
    updated_at: '2024-06-01 00:10:02',
    message_count: 3,
    last_message: newReply,
    settings: { ...session.settings },
  };

  const storage = new Map([['gal2voice.chat.v1', JSON.stringify(storedSessionSnapshot(session))]]);
  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
    fetch: async (url) => {
      const target = String(url);
      if (target.includes('/api/chat/sessions')) {
        return Response.json({ sessions: [server], has_more: false, count: 1 });
      }
      if (target.includes('/api/chat/history')) {
        return Response.json({
          session_id: session.id,
          has_more: false,
          oldest_id: 10,
          count: serverMessages.length,
          messages: serverMessages,
        });
      }
      if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
      if (target.includes('/api/providers')) return Response.json({
        providers: [{ id: 'p1', name: '测试模型', api_key: 'sk-test', chat_model: 'gpt-4o', is_active: true }],
        active_provider: 'p1',
      });
      if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
      if (target.includes('/api/system/engine-status')) return Response.json({ state: 'ready' });
      return Response.json({});
    },
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);

  const fakeAudioBlob = { url: 'blob:http://localhost/fake_user_voice', dur: 1.5 };
  audioStore.set('m_local_failed_prompt', fakeAudioBlob);
  const app = initApp({ dom: { advRoot: root }, renderSessionList() {} });
  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    audioStore.delete('m_local_failed_prompt');
    audioStore.delete('m_11');
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });

  await app.ready;
  for (let i = 0; i < 5; i++) await tick();

  const activeSession = storedActive();
  assert.equal(activeSession.messages.filter(m => m.content === newPrompt).length, 1, '提问在服务端已保存时只应出现一次');
  assert.equal(activeSession.messages.length, 3, '会话应包含历史消息、新提问与新回复共3条');

  const pairedUser = activeSession.messages[1];
  assert.equal(pairedUser.id, 'm_11');
  assert.equal(pairedUser.serverMessageId, 11);
  assert.equal(pairedUser.failed, undefined, '已成功配对服务端的提问不应带有 failed');
  assert.equal(pairedUser.voiceKey, 'user_m_local_failed_prompt', '应保留本地录音元数据');
  assert.equal(pairedUser.dur, 1.5);
  assert.equal(audioStore.get('m_11'), fakeAudioBlob, '配对后应将本地录音 blob 映射同步至服务端消息 ID');

  const assistantReply = activeSession.messages[2];
  assert.equal(assistantReply.id, 'm_12');
  assert.equal(assistantReply.role, 'assistant');
  assert.equal(assistantReply.content, newReply);
  assert.equal(assistantReply.failed, undefined);
  assert.equal(app.conversation.canRetry(pairedUser), false);
  assert.equal(app.conversation.canRetry(assistantReply), false);
});

test('startup sync preserves failed local prompt at end when content matches earlier history but server has no new messages', async t => {
  const doc = fakeDocument();
  globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);

  const repeatedPrompt = '重复历史提问';
  const initialMessages = [
    { id: 'm_1', serverMessageId: 1, role: 'user', content: repeatedPrompt, ts: 1_700_000_000_000 },
    { id: 'm_2', serverMessageId: 2, role: 'assistant', content: '历史回复', ts: 1_700_000_001_000 },
  ];
  const session = {
    id: 's_failed_prompt_same_as_history',
    title: '历史同文未发送成功会话',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_999_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
    messages: [
      ...initialMessages,
      {
        id: 'm_local_failed_tail',
        role: 'user',
        content: repeatedPrompt,
        failed: { error: '网络超时，发送失败', code: 'timeout' },
        voiceKey: 'user_m_local_failed_tail',
        dur: 2.0,
        ts: 1_700_000_999_000,
      },
    ],
  };

  const serverMessages = [
    { id: 1, role: 'user', content_chinese: repeatedPrompt, content_japanese: '', created_at: '2024-06-01 00:00:00' },
    { id: 2, role: 'assistant', content_chinese: '历史回复', content_japanese: '', created_at: '2024-06-01 00:00:01' },
  ];

  const server = {
    id: session.id,
    title: session.title,
    voice_profile_id: null,
    created_at: '2024-06-01 00:00:00',
    updated_at: '2024-06-01 00:00:01',
    message_count: 2,
    last_message: '历史回复',
    settings: { ...session.settings },
  };

  const storage = new Map([['gal2voice.chat.v1', JSON.stringify(storedSessionSnapshot(session))]]);
  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
    fetch: async (url) => {
      const target = String(url);
      if (target.includes('/api/chat/sessions')) {
        return Response.json({ sessions: [server], has_more: false, count: 1 });
      }
      if (target.includes('/api/chat/history')) {
        return Response.json({
          session_id: session.id,
          has_more: false,
          oldest_id: 1,
          count: serverMessages.length,
          messages: serverMessages,
        });
      }
      if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
      if (target.includes('/api/providers')) return Response.json({
        providers: [{ id: 'p1', name: '测试模型', api_key: 'sk-test', chat_model: 'gpt-4o', is_active: true }],
        active_provider: 'p1',
      });
      if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
      if (target.includes('/api/system/engine-status')) return Response.json({ state: 'ready' });
      return Response.json({});
    },
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);

  const app = initApp({ dom: { advRoot: root }, renderSessionList() {} });
  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });

  await app.ready;
  for (let i = 0; i < 5; i++) await tick();

  const activeSession = storedActive();
  assert.equal(activeSession.messages.length, 3, '应保留历史2条 + 末尾1条未发送成功的失败提问');
  const tailMsg = activeSession.messages.at(-1);
  assert.equal(tailMsg.id, 'm_local_failed_tail');
  assert.equal(tailMsg.content, repeatedPrompt);
  assert.ok(tailMsg.failed, '未入库的新提问必须保持 failed 状态');
  assert.equal(app.conversation.canRetry(tailMsg), true, '末尾失败提问必须支持重试');
});

test('startup sync reconciles multiple unpersisted failed prompts correctly when only a subset was persisted on server', async t => {
  const doc = fakeDocument();
  globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);

  const initialMessages = [
    { id: 'm_10', serverMessageId: 10, role: 'assistant', content: '之前历史消息', ts: 1_700_000_000_000 },
  ];
  const session = {
    id: 's_multi_failed_prompts',
    title: '多条失败提问部分落库会话',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_999_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
    messages: [
      ...initialMessages,
      {
        id: 'm_local_failed_1',
        role: 'user',
        content: '第一条提问已落库',
        failed: { error: '流式断开', code: 'cancelled' },
        voiceKey: 'user_m_local_failed_1',
        dur: 1.2,
        ts: 1_700_000_998_000,
      },
      {
        id: 'm_local_failed_2',
        role: 'user',
        content: '第二条提问未落库',
        failed: { error: '网络中断', code: 'timeout' },
        voiceKey: 'user_m_local_failed_2',
        dur: 2.1,
        ts: 1_700_000_999_000,
      },
    ],
  };

  const serverMessages = [
    { id: 10, role: 'assistant', content_chinese: '之前历史消息', content_japanese: '', created_at: '2024-06-01 00:00:00' },
    { id: 11, role: 'user', content_chinese: '第一条提问已落库', content_japanese: '', created_at: '2024-06-01 00:10:01' },
    { id: 12, role: 'assistant', content_chinese: '第一条回复', content_japanese: '返答12', created_at: '2024-06-01 00:10:02' },
  ];

  const server = {
    id: session.id,
    title: session.title,
    voice_profile_id: null,
    created_at: '2024-06-01 00:00:00',
    updated_at: '2024-06-01 00:10:02',
    message_count: 3,
    last_message: '第一条回复',
    settings: { ...session.settings },
  };

  const storage = new Map([['gal2voice.chat.v1', JSON.stringify(storedSessionSnapshot(session))]]);
  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
    fetch: async (url) => {
      const target = String(url);
      if (target.includes('/api/chat/sessions')) {
        return Response.json({ sessions: [server], has_more: false, count: 1 });
      }
      if (target.includes('/api/chat/history')) {
        return Response.json({
          session_id: session.id,
          has_more: false,
          oldest_id: 10,
          count: serverMessages.length,
          messages: serverMessages,
        });
      }
      if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
      if (target.includes('/api/providers')) return Response.json({
        providers: [{ id: 'p1', name: '测试模型', api_key: 'sk-test', chat_model: 'gpt-4o', is_active: true }],
        active_provider: 'p1',
      });
      if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
      if (target.includes('/api/system/engine-status')) return Response.json({ state: 'ready' });
      return Response.json({});
    },
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);

  const app = initApp({ dom: { advRoot: root }, renderSessionList() {} });
  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });

  await app.ready;
  for (let i = 0; i < 5; i++) await tick();

  const activeSession = storedActive();
  assert.equal(activeSession.messages.length, 4, '应包含历史1条 + 新问答2条 + 末尾1条未落库失败提问');

  const pairedUser = activeSession.messages[1];
  assert.equal(pairedUser.id, 'm_11');
  assert.equal(pairedUser.serverMessageId, 11);
  assert.equal(pairedUser.content, '第一条提问已落库');
  assert.equal(pairedUser.failed, undefined);
  assert.equal(pairedUser.voiceKey, 'user_m_local_failed_1');

  const assistantReply = activeSession.messages[2];
  assert.equal(assistantReply.id, 'm_12');
  assert.equal(assistantReply.content, '第一条回复');
  assert.equal(assistantReply.failed, undefined);

  const tailMsg = activeSession.messages[3];
  assert.equal(tailMsg.id, 'm_local_failed_2');
  assert.equal(tailMsg.content, '第二条提问未落库');
  assert.ok(tailMsg.failed, '未落库的第二条提问应保留在末尾且保持 failed 状态');
  assert.equal(app.conversation.canRetry(tailMsg), true, '末尾未落库失败提问必须支持重试');
});

for (const totalServerMessages of [150, 101]) {
  test(`startup sync preserves unanswered local prompt and enables retry when server has ${totalServerMessages} messages (>100)`, async t => {
    const doc = fakeDocument();
    globalThis.document = doc;
    doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
    const root = doc.createElement('div');
    root.id = 'advRoot';
    doc.body.appendChild(root);
    const toasts = doc.createElement('div');
    toasts.id = 'toastRoot';
    doc.body.appendChild(toasts);

    const oldestWindowId = totalServerMessages - 100 + 1;
    const serverWindow = Array.from({ length: 100 }, (_, idx) => {
      const id = oldestWindowId + idx;
      const isAssistant = (totalServerMessages - id) % 2 === 0;
      return {
        id,
        role: isAssistant ? 'assistant' : 'user',
        content_chinese: isAssistant ? `服务端回复 #${id}` : `历史提问 #${id}`,
        content_japanese: '',
        created_at: '2024-06-01 00:00:00',
      };
    });

    const localCachedWindow = serverWindow.map((bm, idx) => ({
      id: `m_${bm.id}`,
      serverMessageId: bm.id,
      role: bm.role,
      content: bm.content_chinese,
      ts: 1_700_000_000_000 + idx * 1000,
    }));
    const unansweredId = `m_local_unanswered_${totalServerMessages}`;
    const unansweredPrompt = '刷新前刚发出的长会话提问';

    const session = {
      id: `s_long_unanswered_${totalServerMessages}`,
      title: '超百条长会话',
      createdAt: 1_700_000_000_000,
      updatedAt: 1_700_000_999_000,
      hasMoreHistory: true,
      oldestMessageId: oldestWindowId,
      settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
      messages: [
        ...localCachedWindow,
        {
          id: unansweredId,
          role: 'user',
          content: unansweredPrompt,
          voiceKey: `user_${unansweredId}`,
          dur: 1.6,
          ts: 1_700_000_999_000,
        },
      ],
    };

    const server = {
      id: session.id,
      title: session.title,
      voice_profile_id: null,
      created_at: '2024-06-01 00:00:00',
      updated_at: '2024-06-01 00:10:00',
      message_count: totalServerMessages,
      last_message: `服务端回复 #${totalServerMessages}`,
      settings: { ...session.settings },
    };

    const storage = new Map([['gal2voice.chat.v1', JSON.stringify(storedSessionSnapshot(session))]]);
    globalThis.localStorage = {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key),
    };
    globalThis.location = { origin: 'http://127.0.0.1:8000' };

    let historyFetches = 0;
    const streamRequests = [];
    globalThis.window = {
      innerWidth: 1280,
      location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
      sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
      prompt: () => null,
      fetch: async (url, options = {}) => {
        const target = String(url);
        if (target.includes('/api/chat/sessions')) {
          return Response.json({ sessions: [server], has_more: false, count: 1 });
        }
        if (target.includes('/api/chat/history')) {
          historyFetches += 1;
          return Response.json({
            session_id: session.id,
            has_more: true,
            oldest_id: oldestWindowId,
            count: serverWindow.length,
            messages: serverWindow,
          });
        }
        if (target.includes('/api/chat/stream')) {
          let output;
          const body = new ReadableStream({
            start(controller) { output = controller; },
            cancel() { try { output?.close(); } catch {} },
          });
          options.signal?.addEventListener('abort', () => {
            try { output?.close(); } catch {}
          }, { once: true });
          streamRequests.push({ url: target, prompt: JSON.parse(options.body).prompt });
          return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } });
        }
        if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
        if (target.includes('/api/providers')) return Response.json({
          providers: [{ id: 'p1', name: '测试模型', api_key: 'sk-test', chat_model: 'gpt-4o', is_active: true }],
          active_provider: 'p1',
        });
        if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
        if (target.includes('/api/system/engine-status')) return Response.json({ state: 'ready' });
        return Response.json({});
      },
    };
    globalThis.fetch = (...args) => globalThis.window.fetch(...args);

    const app = initApp({ dom: { advRoot: root }, renderSessionList() {} });
    t.after(() => {
      try { app.conversation.stop(); app.adv?.unmount(); } catch {}
      for (const toast of toasts.children) clearTimeout(toast._timer);
      delete globalThis.document;
      delete globalThis.window;
      delete globalThis.localStorage;
      delete globalThis.fetch;
      delete globalThis.location;
    });

    await app.ready;
    for (let i = 0; i < 5; i++) await tick();

    assert.equal(historyFetches, 1, `服务端 message_count=${totalServerMessages} 时应拉取最近 100 条历史做核对`);
    const activeSession = storedActive();
    assert.equal(activeSession.hasMoreHistory, true);
    assert.equal(activeSession.oldestMessageId, oldestWindowId);
    assert.equal(activeSession.messages.length, 101, '应保留最近 100 条服务端消息 + 末尾 1 条未获回复本地提问');

    const last = activeSession.messages.at(-1);
    assert.equal(last.id, unansweredId);
    assert.equal(last.content, unansweredPrompt);
    assert.equal(last.voiceKey, `user_${unansweredId}`);
    assert.equal(Boolean(last.failed), true, '未获回复本地提问必须标为 failed');
    assert.deepEqual(last.failed, { error: '对话已中断，未收到回复', code: 'cancelled' });
    assert.equal(app.conversation.canRetry(last), true, '末尾失败提问必须支持重试');

    const storedJson = JSON.parse(storage.get('gal2voice.chat.v1'));
    const storedSess = storedJson.sessions.find(s => s.id === session.id);
    assert.equal(storedSess.messages.length, 101);
    assert.deepEqual(storedSess.messages.at(-1).failed, { error: '对话已中断，未收到回复', code: 'cancelled' });

    app.adv.action('log');
    const retryBtn = [...doc.querySelectorAll('.adv-error-btn')].find(b => b.textContent === '重新发送');
    assert.ok(retryBtn, 'LOG 面板中必须渲染「重新发送」按钮');
    retryBtn.click();
    for (let i = 0; i < 5 && streamRequests.length === 0; i++) await tick();
    assert.equal(streamRequests.length, 1, '点击「重新发送」应向 /api/chat/stream 发起重试请求');
    assert.equal(streamRequests[0].prompt, unansweredPrompt);
    app.conversation.stop();
  });
}

test('startup sync in >100 message session merges answered local prompt without duplication or false failure', async t => {
  const doc = fakeDocument();
  globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);

  const initialWindow = Array.from({ length: 100 }, (_, idx) => {
    const id = 51 + idx;
    const isAssistant = id % 2 === 0;
    return {
      id: `m_${id}`,
      serverMessageId: id,
      role: isAssistant ? 'assistant' : 'user',
      content: isAssistant ? `服务端回复 #${id}` : `历史提问 #${id}`,
      ts: 1_700_000_000_000 + idx * 1000,
    };
  });

  const answeredPrompt = '刷新期间已完成回复的提问';
  const answeredReply = '服务端已生成的第152条回复';
  const session = {
    id: 's_long_already_answered',
    title: '超百条已完成问答会话',
    createdAt: 1_700_000_000_000,
    updatedAt: 1_700_000_999_000,
    hasMoreHistory: true,
    oldestMessageId: 51,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null },
    messages: [
      ...initialWindow,
      {
        id: 'm_local_already_answered',
        role: 'user',
        content: answeredPrompt,
        voiceKey: 'user_m_local_already_answered',
        dur: 2.4,
        ts: 1_700_000_999_000,
      },
    ],
  };

  const serverWindow = Array.from({ length: 100 }, (_, idx) => {
    const id = 53 + idx;
    if (id === 151) {
      return { id: 151, role: 'user', content_chinese: answeredPrompt, content_japanese: '', created_at: '2024-06-01 00:10:01' };
    }
    if (id === 152) {
      return { id: 152, role: 'assistant', content_chinese: answeredReply, content_japanese: '返答152', created_at: '2024-06-01 00:10:02' };
    }
    const isAssistant = id % 2 === 0;
    return {
      id,
      role: isAssistant ? 'assistant' : 'user',
      content_chinese: isAssistant ? `服务端回复 #${id}` : `历史提问 #${id}`,
      content_japanese: '',
      created_at: '2024-06-01 00:00:00',
    };
  });

  const server = {
    id: session.id,
    title: session.title,
    voice_profile_id: null,
    created_at: '2024-06-01 00:00:00',
    updated_at: '2024-06-01 00:10:02',
    message_count: 152,
    last_message: answeredReply,
    settings: { ...session.settings },
  };

  const storage = new Map([['gal2voice.chat.v1', JSON.stringify(storedSessionSnapshot(session))]]);
  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
    fetch: async (url) => {
      const target = String(url);
      if (target.includes('/api/chat/sessions')) {
        return Response.json({ sessions: [server], has_more: false, count: 1 });
      }
      if (target.includes('/api/chat/history')) {
        return Response.json({
          session_id: session.id,
          has_more: true,
          oldest_id: 53,
          count: 100,
          messages: serverWindow,
        });
      }
      if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
      if (target.includes('/api/providers')) return Response.json({
        providers: [{ id: 'p1', name: '测试模型', api_key: 'sk-test', chat_model: 'gpt-4o', is_active: true }],
        active_provider: 'p1',
      });
      if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
      if (target.includes('/api/system/engine-status')) return Response.json({ state: 'ready' });
      return Response.json({});
    },
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);

  const app = initApp({ dom: { advRoot: root }, renderSessionList() {} });
  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });

  await app.ready;
  for (let i = 0; i < 5; i++) await tick();

  const activeSession = storedActive();
  assert.equal(activeSession.messages.length, 100, '服务端已完成该轮问答时不应重复追加本地提问');
  assert.equal(activeSession.messages.filter(m => m.content === answeredPrompt).length, 1);

  const pairedUser = activeSession.messages.at(-2);
  assert.equal(pairedUser.id, 'm_151');
  assert.equal(pairedUser.serverMessageId, 151);
  assert.equal(pairedUser.failed, undefined, '已完成问答的提问不得误标 failed');
  assert.equal(pairedUser.voiceKey, 'user_m_local_already_answered', '已完成问答的提问应保留本地录音元数据');
  assert.equal(pairedUser.dur, 2.4);

  const assistantReply = activeSession.messages.at(-1);
  assert.equal(assistantReply.id, 'm_152');
  assert.equal(assistantReply.role, 'assistant');
  assert.equal(assistantReply.content, answeredReply);
  assert.equal(assistantReply.failed, undefined);
  assert.equal(app.conversation.canRetry(pairedUser), false);
  assert.equal(app.conversation.canRetry(assistantReply), false);
});

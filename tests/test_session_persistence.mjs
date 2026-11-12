import assert from 'node:assert/strict';
import fs from 'node:fs';
import { persistSession, deletePersistedSession, isSessionDeleting, fetchAllSessions, fetchSessionHistoryPage, earliestServerMessageId } from '../frontend/src/session_api.js';
import { mergeChatBackup, exportCompleteChatBackup } from '../frontend/src/chat_backup.js';
import { pickSettings, serverTimestamp, normalizeSnapshot, portableSnapshot } from '../frontend/src/session_data.js';
import { renderSessionList } from '../frontend/src/ui.js';
import { streamChat } from '../frontend/src/ai.js';
import { DEFAULT_GLOBAL, DEFAULT_SESSION_SETTINGS, GREETING, LEGACY_GREETINGS, state, loadState, saveState, getSession, getActive, exportSessionData, prepareSessionImport, applySessionImport } from '../frontend/src/store.js';

const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const response = data => ({ ok: true, json: async () => data });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
let passed = 0;

{
  const defaults = { advPlayerName: '我' };
  assert.equal(pickSettings({ advPlayerName: 'Player' }, defaults).advPlayerName, '我');
  assert.equal(pickSettings({ advPlayerName: '小明' }, defaults).advPlayerName, '小明');
  assert.equal(pickSettings({}, defaults).advPlayerName, '我'); passed++;
}

{
  const cleanedGlobal = pickSettings({ voiceMode: true, advSubtitleLanguage: 'zh' }, DEFAULT_GLOBAL);
  assert.equal('advSubtitleLanguage' in cleanedGlobal, false);
  assert.equal(cleanedGlobal.advSubtitleLanguage, undefined);

  const cleanedGlobalEmpty = pickSettings({}, DEFAULT_GLOBAL);
  assert.equal('advSubtitleLanguage' in cleanedGlobalEmpty, false);
  assert.equal(cleanedGlobalEmpty.advSubtitleLanguage, undefined);

  const fixedSession = pickSettings({ advPrimaryLanguage: 'zh', advSubtitleLanguage: 'zh' }, DEFAULT_SESSION_SETTINGS);
  assert.equal(fixedSession.advSubtitleLanguage, 'ja');

  // mergeChatBackup strips polluted advSubtitleLanguage from merged export global
  const serverBkp = {
    format: 'galgame2voice.chat', version: 1,
    sessions: [{ id: 's_srv', title: 's', createdAt: 1, updatedAt: 2, settings: {}, messages: [] }],
    global: { voiceMode: true, advSubtitleLanguage: 'zh' },
  };
  const localBkp = {
    format: 'galgame2voice.chat', version: 1,
    sessions: [{ id: 's_loc', title: 'l', createdAt: 1, updatedAt: 2, settings: {}, messages: [] }],
    global: { voiceMode: true, advSubtitleLanguage: 'zh' },
  };
  const merged = mergeChatBackup(serverBkp, localBkp);
  assert.equal('advSubtitleLanguage' in JSON.parse(merged.content).global, false, 'mergeChatBackup must not leak advSubtitleLanguage in global');
  passed++;
}

{
  const expected = Date.UTC(2024, 2, 2, 1, 2, 3, 456);
  assert.equal(serverTimestamp('2024-03-02 01:02:03.456'), expected);
  assert.equal(serverTimestamp('2024-03-02T09:02:03.456+08:00'), expected);
  assert.equal(serverTimestamp('2024-03-02T01:02:03.456Z'), expected);
  assert.equal(serverTimestamp('invalid', 12), 12); passed++;
}

{
  const first = deferred();
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, body: options.body && JSON.parse(options.body) });
    return calls.length === 1 ? first.promise : response({});
  };
  const session = { id: 'ordered', title: '旧标题', settings: { temperature: 0.2 } };
  const older = persistSession(session);
  session.title = '新标题'; session.settings.temperature = 0.8;
  const newer = persistSession(session);
  session.title = '未保存草稿';
  await tick(); assert.equal(calls.length, 1);
  first.resolve(response({})); await Promise.all([older, newer]);
  assert.deepEqual(calls.map(call => call.body.title), ['旧标题', '新标题']);
  assert.deepEqual(calls.map(call => call.body.settings.temperature), [0.2, 0.8]); passed++;
}
{
  const save = deferred(); const calls = [];
  globalThis.fetch = async (_url, options) => { calls.push(options.method); return options.method === 'POST' ? save.promise : response({ status: 'deleted' }); };
  const saving = persistSession({ id: 'deletion', settings: {} });
  const removing = deletePersistedSession('deletion');
  assert.equal(deletePersistedSession('deletion'), removing);
  assert.equal(isSessionDeleting('deletion'), true);
  await assert.rejects(persistSession({ id: 'deletion' }), /删除/);
  assert.deepEqual(calls, ['POST']);
  save.resolve(response({})); await Promise.all([saving, removing]);
  assert.deepEqual(calls, ['POST', 'DELETE']);
  assert.equal(isSessionDeleting('deletion'), false);
  await assert.rejects(persistSession({ id: 'deletion' }), /删除/); passed++;
}
{
  globalThis.fetch = async () => { throw new TypeError('offline'); };
  await assert.rejects(deletePersistedSession('retry'));
  assert.equal(isSessionDeleting('retry'), false);
  globalThis.fetch = async () => response({});
  await persistSession({ id: 'retry', settings: {} });
  await deletePersistedSession('retry'); passed++;
}
{
  const first = deferred(); let count = 0;
  globalThis.fetch = async () => ++count === 1 ? first.promise : response({});
  const failed = persistSession({ id: 'failed-save' });
  const rejected = assert.rejects(failed);
  const next = persistSession({ id: 'failed-save', title: 'Retry' });
  first.reject(new Error('offline')); await rejected; await next;
  assert.equal(count, 2); passed++;
}
{
  const calls = [];
  globalThis.fetch = async url => {
    calls.push(url);
    return response({ sessions: [{ id: calls.length }], has_more: calls.length === 1 });
  };
  assert.deepEqual(await fetchAllSessions(), [{ id: 1 }, { id: 2 }]);
  assert.match(calls[1], /offset=200/); passed++;
}
const message = (n, content = `消息 ${n}`) => ({ id: `m_${n}`, role: 'user', content, ts: n });
const session = (id, messages) => ({ id, title: id, messages, settings: { temperature: 0.7 }, createdAt: 1, updatedAt: 2 });
const backup = sessions => ({ format: 'galgame2voice.chat', version: 1, sessions, global: {}, activeId: sessions[0]?.id });
{
  const full = backup(Array.from({ length: 80 }, (_, index) => session(`s_${index}`, Array.from({ length: 600 }, (_, n) => message(n)))));
  const local = backup([session('s_0', full.sessions[0].messages.slice(-100))]);
  const result = mergeChatBackup(full, local);
  const exported = JSON.parse(result.content);
  assert.equal(exported.sessions.length, 80);
  assert.equal(exported.sessions[0].messages.length, 600);
  assert.equal(result.browserCopies, 0); passed++;
}
{
  const server = backup([session('same', [message(1, '你好'), message(2, '你好')])]);
  const local = backup([session('same', [message(3, '你好'), message(4, '你好'), message(5, '尚未同步')])]);
  const original = JSON.stringify([server, local]);
  const result = mergeChatBackup(server, local);
  const exported = JSON.parse(result.content);
  assert.equal(result.browserCopies, 1);
  assert.deepEqual(exported.sessions.map(s => s.messages.length), [2, 3]);
  assert.match(exported.sessions[1].title, /浏览器副本/);
  assert.equal(JSON.stringify([server, local]), original); passed++;
}
{
  const full = backup([session('repeat', [message(1, '同一句'), message(2, '同一句')])]);
  const local = backup([session('repeat', [message(8, '同一句'), message(9, '同一句')])]);
  assert.equal(JSON.parse(mergeChatBackup(full, local).content).sessions[0].messages.length, 2);
  local.sessions[0].messages.push(message(10, '同一句'));
  assert.equal(mergeChatBackup(full, local).browserCopies, 1); passed++;
}
{
  const server = backup([session('old', [message(1)])]);
  const local = backup([session('local-only', [message(2)])]);
  local.sessions[0].settings.apiKey = 'private'; local.sessions[0].messages[0].audioUrls = ['/audio/file.wav'];
  const result = mergeChatBackup(server, local);
  assert.equal(result.count, 2);
  assert.ok(!result.content.includes('private') && !result.content.includes('/audio/'));
  assert.equal(JSON.parse(result.content).activeId, 'local-only'); passed++;
}
{
  globalThis.fetch = async () => { throw new TypeError('offline'); };
  await assert.rejects(exportCompleteChatBackup()); passed++;
}
{
  const local = backup([session('no-voice', [{...message(1, '不能丢弃的无语音回复'), role:'assistant', noVoice:true}])]);
  const result = mergeChatBackup(backup([session('no-voice', [])]), local);
  assert.equal(result.browserCopies, 1);
  assert.ok(result.content.includes('不能丢弃的无语音回复')); passed++;
}
{
  // LOG and the title screen print the greeting as plain text, and the old chat layout is gone.
  assert.doesNotMatch(GREETING, /\*\*|顶栏|侧边栏|右上角/);
  assert.match(GREETING, /设置中心/);
  assert.match(GREETING, /返回首页/);
  assert.doesNotMatch(GREETING, /CONFIG 面板的底部/);
  // A session that still holds an older greeting is not exported as an extra browser copy.
  for (const greeting of [GREETING, ...LEGACY_GREETINGS]) {
    const local = backup([session('greeted', [{ ...message(0, greeting), role: 'assistant', noVoice: true }, message(1)])]);
    const result = mergeChatBackup(backup([session('greeted', [message(1)])]), local);
    assert.equal(result.browserCopies, 0);
  }
  passed++;
}
{
  // Sessions created before wording unification containing "返回开始页" must be recognized as legacy greetings
  // so exportCompleteChatBackup / mergeChatBackup does not treat them as extra local messages, avoids creating
  // an unwanted browser copy, and preserves newer local session settings.
  const legacyGreetingWithReturnStart =
    '欢迎使用 Gal2Voice。\n\n轮到你时，在文本框里打字后回车发送，或点麦克风说话。默认语音同步模式会等本句声音开始再出字，AUTO 等说完再翻句；点击画面也可先看文字。\n\n首页齿轮和聊天菜单的设置按钮都打开同一个设置中心：“角色与会话”选择音色和人设，“聊天体验”调整字幕与播放方式，“对话模型”配置模型。窗口顶部可关闭设置或返回开始页。\n\n每个会话的上下文互相独立，切换回来时记录原样保留。';
  assert.ok(LEGACY_GREETINGS.includes(legacyGreetingWithReturnStart));
  const localSession = session('greeted', [{ ...message(0, legacyGreetingWithReturnStart), role: 'assistant', noVoice: true }, message(1)]);
  localSession.settings = { ...localSession.settings, advPlayerName: '新名' };
  localSession.updatedAt = 200;
  const serverSession = session('greeted', [message(1)]);
  serverSession.settings = { ...serverSession.settings, advPlayerName: '旧名' };
  serverSession.updatedAt = 100;
  const result = mergeChatBackup(backup([serverSession]), backup([localSession]));
  assert.equal(result.browserCopies, 0);
  const parsed = JSON.parse(result.content);
  assert.equal(parsed.sessions[0].settings.advPlayerName, '新名');
  passed++;
}
{
  assert.equal(earliestServerMessageId([{ id: 'm_stream_1' }, { id: 'm_42' }, { id: 'm_15' }, { id: 'm_99' }]), 15);
  assert.equal(earliestServerMessageId([{ id: 'm_local_abc' }]), null);
  const calls = [];
  globalThis.fetch = async url => {
    calls.push(url);
    return response({ messages: [{ id: 5, role: 'user', content_chinese: '更早消息' }], has_more: true, oldest_id: 5 });
  };
  const page = await fetchSessionHistoryPage('s_long', { limit: 100, beforeId: 15 });
  assert.equal(page.has_more, true);
  assert.equal(page.oldest_id, 5);
  assert.match(calls[0], /session_id=s_long/);
  assert.match(calls[0], /before_id=15/);
  passed++;
}
{
  const bodies = [];
  globalThis.fetch = async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    return new Response('event: done\ndata: {"chinese":"收到"}\n\n', { headers: { 'Content-Type': 'text/event-stream' } });
  };
  const sessions = [
    { id: 's_legacy', settings: { advReplyFormat: 'legacy' } },
    { id: 's_old', settings: { advReplyFormat: 'old' } },
    { id: 's_lines', settings: { advReplyFormat: 'lines', advOutfit: 'casual' } },
    { id: 's_unset', settings: {} },
  ];
  for (const active of sessions) {
    await new Promise((resolve) => streamChat({
      prompt: '你好', sessionId: active.id, settings: active.settings,
      onChunk() {}, onEnd: () => resolve(), fetch: globalThis.fetch,
    }));
  }
  // All sessions request per-line replies even if stale advReplyFormat values remain on settings.
  assert.deepEqual(bodies.map(body => [body.session_id, body.reply_format, body.outfit]), [
    ['s_legacy', 'lines', undefined], ['s_old', 'lines', undefined],
    ['s_lines', 'lines', 'casual'], ['s_unset', 'lines', undefined],
  ]); passed++;
}

// The export button calls exportCompleteChatBackup, which reads /api/chat/export
// and the in-memory browser snapshot. A prefix must not drop the newer settings.
function installChatStorage(session) {
  const values = new Map([['gal2voice.chat.v1', JSON.stringify({
    sessions: [session], activeId: session.id, global: {},
  })]]);
  globalThis.localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  };
}
const prefixMessages = [
  { id: 'm_10', role: 'user', content: '今天去哪', japanese: '', ts: 1_700_000_000_000, lines: null },
  { id: 'm_11', role: 'assistant', content: '去江边走走吧', japanese: '海辺へ', ts: 1_700_000_002_000, lines: null },
];
async function exportPrefix(localSettings, localUpdatedAt) {
  installChatStorage({
    id: 's_backup', title: '晚上的对话', createdAt: 1_700_000_000_000, updatedAt: localUpdatedAt,
    settings: localSettings,
    messages: [{ id: 'm_local_user', role: 'user', content: '今天去哪', ts: 1_700_000_000_000 }],
  });
  loadState();
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return response({
      format: 'galgame2voice.chat', version: 1, exportedAt: '2024-06-01T00:00:02.000Z',
      activeId: 's_backup', global: {},
      sessions: [{
        id: 's_backup', title: '晚上的对话', createdAt: 1_700_000_000_000, updatedAt: 1_700_000_500_000,
        settings: { advPlayerName: '小明', advWindowOpacity: 40, voiceProfileId: 3 },
        messages: prefixMessages,
      }],
    });
  };
  const result = await exportCompleteChatBackup();
  assert.deepEqual(calls, ['/api/chat/export']);
  const exported = JSON.parse(result.content).sessions[0];
  const exportedGlobal = JSON.parse(result.content).global;
  assert.equal('advSubtitleLanguage' in exportedGlobal, false, 'exportCompleteChatBackup should not contain advSubtitleLanguage in global');
  return { result, exported };
}
{
  // complete export keeps newer server settings when local messages are only a prefix
  const { result, exported } = await exportPrefix(
    { advPlayerName: '旧名', advWindowOpacity: 80, voiceProfileId: null }, 1_700_000_100_000);
  assert.equal(result.browserCopies, 0);
  assert.deepEqual(exported.messages.map(message => message.content), ['今天去哪', '去江边走走吧']);
  assert.equal(exported.settings.advPlayerName, '小明');
  assert.equal(exported.settings.advWindowOpacity, 40);
  assert.equal(exported.settings.voiceProfileId, 3);
  passed++;
}
{
  // complete export keeps newer local settings when the browser copy was saved later
  const { result, exported } = await exportPrefix(
    { advPlayerName: '新名', advWindowOpacity: 30, voiceProfileId: 8 }, 1_700_000_900_000);
  assert.equal(result.browserCopies, 0);
  assert.deepEqual(exported.messages.map(message => message.content), ['今天去哪', '去江边走走吧']);
  assert.equal(exported.settings.advPlayerName, '新名');
  assert.equal(exported.settings.advWindowOpacity, 30);
  assert.equal(exported.settings.voiceProfileId, 8);
  passed++;
}
{
  const html = fs.readFileSync(new URL('../frontend/index.html', import.meta.url), 'utf8');
  assert.ok(!html.includes('gAutoTranslate'), 'index.html should not contain gAutoTranslate');
  assert.ok(!html.includes('手动（点击消息底部的“翻译”按钮）'), 'index.html should not contain dead translate option');
  assert.ok(!html.includes('日文原文显示'), 'index.html should not contain 日文原文显示 label');
  passed++;
}
{
  const legacyBackup = {
    format: 'galgame2voice.chat',
    version: 1,
    activeId: 's_legacy',
    global: { voiceMode: true, ttsPreset: 'default', autoTranslate: true },
    sessions: [{
      id: 's_legacy',
      title: '旧会话',
      messages: [{ id: 'm_1', role: 'user', content: '测试', ts: 1 }],
      settings: { temperature: 0.7, autoTranslate: 'manual' },
      createdAt: 1,
      updatedAt: 1,
    }],
  };
  const normalized = normalizeSnapshot(legacyBackup, DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL);
  assert.equal(normalized.global.autoTranslate, undefined, 'obsolete autoTranslate in global must be dropped');
  assert.equal(normalized.sessions[0].settings.autoTranslate, undefined, 'obsolete autoTranslate in session settings must be dropped');
  assert.equal(normalized.global.voiceMode, true);
  assert.equal(normalized.global.ttsPreset, 'default');

  const merged = mergeChatBackup(legacyBackup, legacyBackup);
  const parsed = JSON.parse(merged.content);
  assert.equal(parsed.global.autoTranslate, undefined, 'merged backup must not contain autoTranslate in global');
  assert.equal(parsed.sessions[0].settings.autoTranslate, undefined, 'merged backup must not contain autoTranslate in session settings');
  passed++;
}
{
  class MemoryStorage {
    values = new Map();
    getItem(key) { return this.values.get(key) ?? null; }
    removeItem(key) { this.values.delete(key); }
    setItem(key, value) { this.values.set(key, String(value)); }
  }
  globalThis.localStorage = new MemoryStorage();
  const STORAGE_KEY = 'gal2voice.chat.v1';

  const storedSessions = [
    {
      id: 's_legacy_1',
      title: '旧会话 1',
      createdAt: 1000,
      updatedAt: 1001,
      settings: { temperature: 0.7 },
      messages: [
        { id: 'm_1_1', role: 'assistant', content: LEGACY_GREETINGS[0], noVoice: true, ts: 1000 },
        { id: 'm_1_2', role: 'user', content: '用户发问', ts: 1001 },
        { id: 'm_1_3', role: 'assistant', content: '后续回复保持原样', ts: 1002 },
      ],
    },
    {
      id: 's_legacy_2',
      title: '旧会话 2',
      createdAt: 2000,
      updatedAt: 2001,
      settings: { temperature: 0.7 },
      messages: [
        { id: 'm_2_1', role: 'assistant', content: LEGACY_GREETINGS[1], noVoice: true, ts: 2000 },
      ],
    },
    {
      id: 's_custom',
      title: '自定义开场白会话',
      createdAt: 3000,
      updatedAt: 3001,
      settings: { temperature: 0.7 },
      messages: [
        { id: 'm_3_1', role: 'assistant', content: '自定义开场白，不得被替换', ts: 3000 },
      ],
    },
    {
      id: 's_user_matching',
      title: '用户发了旧欢迎语文案',
      createdAt: 4000,
      updatedAt: 4001,
      settings: { temperature: 0.7 },
      messages: [
        { id: 'm_4_1', role: 'user', content: LEGACY_GREETINGS[0], ts: 4000 },
        { id: 'm_4_2', role: 'assistant', content: '用户输入不得被修改', ts: 4001 },
      ],
    },
  ];

  globalThis.localStorage.setItem(STORAGE_KEY, JSON.stringify({
    sessions: storedSessions,
    activeId: 's_legacy_1',
    global: {},
  }));

  loadState();

  const s1 = getSession('s_legacy_1');
  const s2 = getSession('s_legacy_2');
  const s3 = getSession('s_custom');
  const s4 = getSession('s_user_matching');

  // 1. 旧欢迎语被替换为现行欢迎语
  assert.equal(s1.messages[0].content, GREETING);
  assert.equal(s2.messages[0].content, GREETING);
  assert.equal(getActive().messages[0].content, GREETING);

  // 2. 会话中的其他消息（用户消息与后续助手回复）保持原样
  assert.equal(s1.messages[1].content, '用户发问');
  assert.equal(s1.messages[2].content, '后续回复保持原样');

  // 3. 内容不同的助手消息不得被替换
  assert.equal(s3.messages[0].content, '自定义开场白，不得被替换');

  // 4. 用户输入内容即使与旧欢迎语完全一致也不得被替换
  assert.equal(s4.messages[0].content, LEGACY_GREETINGS[0]);
  assert.equal(s4.messages[1].content, '用户输入不得被修改');

  // 5. 备份导入导出与往返不受影响
  const exportedRaw = exportSessionData();
  const exported = JSON.parse(exportedRaw);
  assert.equal(exported.sessions.find(s => s.id === 's_legacy_1').messages[0].content, GREETING);
  assert.equal(exported.sessions.find(s => s.id === 's_user_matching').messages[0].content, LEGACY_GREETINGS[0]);

  // 导入包含旧欢迎语的历史备份时，同样刷新旧欢迎语并保持往返有效
  const legacyBackupRaw = JSON.stringify({
    format: 'galgame2voice.chat',
    version: 1,
    sessions: [
      {
        id: 's_imported_legacy',
        title: '导入的旧会话',
        createdAt: 5000,
        updatedAt: 5001,
        settings: {},
        messages: [
          { id: 'm_imp_1', role: 'assistant', content: LEGACY_GREETINGS[0], noVoice: true, ts: 5000 },
          { id: 'm_imp_2', role: 'user', content: '导入的用户提问', ts: 5001 },
        ],
      },
    ],
    global: {},
  });
  const importedData = prepareSessionImport(legacyBackupRaw);
  assert.equal(importedData.sessions[0].messages[0].content, GREETING);
  assert.equal(importedData.sessions[0].messages[1].content, '导入的用户提问');
  applySessionImport(importedData);

  // 确保经备份往返及 mergeChatBackup 对比时不会产生额外副本
  const mergeResult = mergeChatBackup(
    backup([session(state.sessions[0].id, [message(1, '导入的用户提问')])]),
    backup([state.sessions[0]])
  );
  assert.equal(mergeResult.browserCopies, 0);

  passed++;
}
{
  class MemoryStorage {
    values = new Map();
    getItem(key) { return this.values.get(key) ?? null; }
    removeItem(key) { this.values.delete(key); }
    setItem(key, value) { this.values.set(key, String(value)); }
  }
  globalThis.localStorage = new MemoryStorage();
  const STORAGE_KEY = 'gal2voice.chat.v1';

  const greeting20260909 =
    '你好，我是 **Gal2Voice**。\n\n支持语音输入与朗读回复：点输入框旁的麦克风说话会自动转成文字，点顶栏喇叭可以朗读我的回复。右上角`会话设置`可配置专属人设与采样参数，侧边栏底部齿轮可切换音色与对话模型。\n\n每个会话的上下文互相独立，切换回来时记录原样保留。';
  const greeting20261003 =
    '你好，我是 **Gal2Voice**。\n\n支持语音输入与朗读回复：点输入框旁的麦克风说话会自动转成文字，点顶栏喇叭可以朗读我的回复。右上角`会话设置`可绑定角色音色并配置人设与采样参数，侧边栏底部齿轮可切换对话模型。\n\n每个会话的上下文互相独立，切换回来时记录原样保留。';

  globalThis.localStorage.setItem(STORAGE_KEY, JSON.stringify({
    sessions: [
      {
        id: 's_hist_1',
        title: '历史会话 1',
        createdAt: 1000,
        updatedAt: 1001,
        settings: {},
        messages: [{ id: 'm_h_1', role: 'assistant', content: greeting20260909, noVoice: true, ts: 1000 }],
      },
      {
        id: 's_hist_2',
        title: '历史会话 2',
        createdAt: 2000,
        updatedAt: 2001,
        settings: {},
        messages: [{ id: 'm_h_2', role: 'assistant', content: greeting20261003, noVoice: true, ts: 2000 }],
      },
    ],
    activeId: 's_hist_1',
    global: {},
  }));

  loadState();

  assert.equal(state.sessions[0].messages[0].content, GREETING);
  assert.equal(state.sessions[1].messages[0].content, GREETING);
  assert.equal(getSession('s_hist_1').messages[0].content, GREETING);
  assert.equal(getSession('s_hist_2').messages[0].content, GREETING);
  const saved = JSON.parse(globalThis.localStorage.getItem(STORAGE_KEY));
  assert.equal(saved.sessions[0].messages[0].content, GREETING);
  assert.equal(saved.sessions[1].messages[0].content, GREETING);
  passed++;
}
{
  function makeElement(tag = 'div') {
    const children = [];
    const attrs = {};
    const dataset = {};
    const style = { setProperty: () => {} };
    const listeners = {};
    return {
      tagName: tag.toUpperCase(), children, dataset, style, className: '', tabIndex: 0, textContent: '', title: '',
      setAttribute(k, v) { attrs[k] = v; }, getAttribute(k) { return attrs[k]; },
      appendChild(c) {
        if (c.tagName === 'FRAGMENT') { children.push(...c.children); c.children.length = 0; return c; }
        children.push(c); return c;
      },
      addEventListener(evt, fn) { (listeners[evt] ||= []).push(fn); },
      set innerHTML(html) {
        children.length = 0;
        if (!html) return;
        if (html.includes('si-main')) {
          const title = makeElement('div'); title.className = 'si-title';
          const preview = makeElement('span'); preview.className = 'si-preview';
          children.push(title, preview);
        }
        if (html.includes('si-del')) {
          const delBtn = makeElement('button'); delBtn.className = 'si-del';
          children.push(delBtn);
        }
      },
      querySelector(sel) {
        for (const child of children) {
          if (sel === '.si-title' && child.className === 'si-title') return child;
          if (sel === '.si-preview' && child.className === 'si-preview') return child;
          if (sel === '.si-del' && child.className === 'si-del') return child;
          const nested = child.querySelector?.(sel);
          if (nested) return nested;
        }
        return null;
      },
      querySelectorAll(sel) {
        const results = [];
        for (const child of children) {
          if (sel === '.si-title' && child.className === 'si-title') results.push(child);
          else if (sel === '.si-preview' && child.className === 'si-preview') results.push(child);
          else if (sel === '.si-del' && child.className === 'si-del') results.push(child);
          else if (sel === '.session-item' && child.className.includes('session-item')) results.push(child);
          if (child.querySelectorAll) results.push(...child.querySelectorAll(sel));
        }
        return results;
      },
    };
  }

  const container = makeElement('div');
  const prevDoc = globalThis.document;
  globalThis.document = {
    createElement: (tag) => makeElement(tag),
    createDocumentFragment: () => makeElement('fragment'),
  };

  try {
    const rawData = {
      sessions: [
        {
          id: 's_sync_lastline',
          title: '已同步历史会话',
          createdAt: 1000,
          updatedAt: 2000,
          messages: [],
          lastLine: '今天天气真不错，我们去散步吧。',
          settings: {},
        },
        {
          id: 's_empty_lastline',
          title: '空消息且无摘要会话',
          createdAt: 1000,
          updatedAt: 2000,
          messages: [],
          lastLine: '',
          settings: {},
        },
        {
          id: 's_active_msgs',
          title: '有完整消息会话',
          createdAt: 1000,
          updatedAt: 2000,
          messages: [{ id: 'm_1', role: 'user', content: '测试消息内容', ts: 1000 }],
          lastLine: '旧的服务端摘要',
          settings: {},
        },
        {
          id: 's_empty_content_msg',
          title: '末条消息内容为空且含摘要',
          createdAt: 1000,
          updatedAt: 2000,
          messages: [{ id: 'm_2', role: 'assistant', content: '', ts: 1000 }],
          lastLine: '后备摘要文本',
          settings: {},
        },
        {
          id: 's_markdown_lastline',
          title: 'Markdown摘要清理截断',
          createdAt: 1000,
          updatedAt: 2000,
          messages: [],
          lastLine: '# 标题*强调*`代码`\n超出二十六个字符之后的内容应当被干净截断显示',
          settings: {},
        },
        {
          id: 's_japanese_lines',
          title: '纯日文台词lines会话',
          createdAt: 1000,
          updatedAt: 2000,
          messages: [
            { id: 'm_ja_u1', role: 'user', content: '你好', ts: 1000 },
            { id: 'm_ja_a1', role: 'assistant', content: '', lines: [{ index: 0, zh: '', ja: 'おかえりなさい！', emotion: 'gentle' }], ts: 1001 },
          ],
          settings: {},
        },
        {
          id: 's_japanese_field',
          title: '纯日文台词japanese属性会话',
          createdAt: 1000,
          updatedAt: 2000,
          messages: [
            { id: 'm_ja_u2', role: 'user', content: '你好', ts: 1000 },
            { id: 'm_ja_a2', role: 'assistant', content: '', japanese: 'ただいま。', ts: 1001 },
          ],
          settings: {},
        },
      ],
      activeId: 's_sync_lastline',
      global: {},
    };

    // 1. normalizeSnapshot 保留合法的非空字符串 lastLine
    const normalized = normalizeSnapshot(rawData, DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL);
    assert.equal(normalized.sessions[0].lastLine, '今天天气真不错，我们去散步吧。', 'normalizeSnapshot 应保留有效 lastLine 字段');
    assert.equal(normalized.sessions[1].lastLine, undefined, '空字符串 lastLine 应被忽略');
    assert.equal(normalized.sessions[2].lastLine, '旧的服务端摘要');
    assert.equal(normalized.sessions[3].lastLine, '后备摘要文本');

    // 2. portableSnapshot 剔除 lastLine 保持导出备份 JSON 结构不变
    const portable = portableSnapshot(normalized);
    assert.equal(portable.sessions[0].lastLine, undefined, 'portableSnapshot 应剥除 lastLine 字段');
    assert.equal(portable.sessions[1].lastLine, undefined);
    assert.equal(portable.sessions[2].lastLine, undefined);
    assert.equal(portable.sessions[3].lastLine, undefined);

    // 3. renderSessionList 渲染预览文本
    renderSessionList(container, {
      sessions: normalized.sessions,
      activeId: 's_active_msgs',
      onSelect: () => {},
      onDelete: () => {},
    });

    const previews = container.querySelectorAll('.si-preview').map(el => el.textContent);
    assert.equal(previews[0], '今天天气真不错，我们去散步吧。', '未拉取消息的同步历史会话应展示 lastLine 摘要而非暂无消息');
    assert.equal(previews[1], '暂无消息', '无消息且无 lastLine 时应展示暂无消息');
    assert.equal(previews[2], '你：测试消息内容', '有完整消息时优先使用最后一条消息');
    assert.equal(previews[3], '后备摘要文本', '最后一条消息内容为空时回退至 lastLine');
    assert.equal(previews[4], '# 标题*强调*`代码`\n超出二十六个字符之后的内容应当被干净截断显示'.replace(/[*`#\n]/g, ' ').slice(0, 26), 'lastLine 同样清理 Markdown 与换行并截断至26字');
    assert.equal(previews[5], 'おかえりなさい！', '最新一条助手消息仅含 lines[].ja 时应展示日文台词摘要而非暂无消息');
    assert.equal(previews[6], 'ただいま。', '最新一条助手消息仅含 japanese 属性时应展示日文台词摘要而非暂无消息');

    passed++;
  } finally {
    globalThis.document = prevDoc;
  }
}
{
  class QuotaStorage {
    values = new Map();
    limit = 100000;
    getItem(key) { return this.values.get(key) ?? null; }
    removeItem(key) { this.values.delete(key); }
    setItem(key, value) {
      if (value.length > this.limit) {
        const err = new Error('quota');
        err.name = 'QuotaExceededError';
        throw err;
      }
      this.values.set(key, String(value));
    }
  }
  const prevStorage = globalThis.localStorage;
  const storage = new QuotaStorage();
  globalThis.localStorage = storage;
  const STORAGE_KEY = 'gal2voice.chat.v1';
  try {
    storage.values.set(STORAGE_KEY, JSON.stringify({
      sessions: [
        {
          id: 's_active_now',
          title: '当前会话',
          createdAt: 2000,
          updatedAt: 2000,
          settings: {},
          messages: [{ id: 'm_act', role: 'user', content: '继续聊天', ts: 2000 }],
        },
        {
          id: 's_inactive_ja_only',
          title: '日文兜底淘汰会话',
          createdAt: 1000,
          updatedAt: 1000,
          settings: {},
          messages: [
            { id: 'm_ja_u', role: 'user', content: '你好', ts: 1000 },
            { id: 'm_ja_a', role: 'assistant', content: '', japanese: 'ただいま。', ts: 1001 },
          ],
        },
      ],
      activeId: 's_active_now',
      global: {},
    }));
    loadState();
    const sInactive = state.sessions.find(s => s.id === 's_inactive_ja_only');
    assert.equal(sInactive.lastLine, undefined);

    const initialLen = storage.values.get(STORAGE_KEY).length;
    const jaMessagesLen = JSON.stringify(sInactive.messages).length;
    storage.limit = initialLen - Math.floor(jaMessagesLen * 0.5);
    assert.equal(saveState(), true);
    assert.equal(sInactive.messages.length, 0, 'inactive session messages evicted');
    assert.equal(sInactive.lastLine, 'ただいま。', '配额超限淘汰 messages 时应回退提取 last.japanese 作为 lastLine');
    passed++;
  } finally {
    globalThis.localStorage = prevStorage;
  }
}
console.log(`Session persistence and complete backups: ${passed} scenarios passed`);

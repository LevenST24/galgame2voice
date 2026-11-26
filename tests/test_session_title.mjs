import assert from 'node:assert/strict';
import { autoTitle, DEFAULT_GLOBAL, DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { normalizeSnapshot } from '../frontend/src/session_data.js';

// A half emoji (lone UTF-16 surrogate) made POST /api/chat/sessions answer 500,
// so the title, every later settings save and any backup holding it failed.
const LONE_SURROGATE = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/;
const EMOJI_AT_19 = '今天下班好累啊想跟你说说话聊聊天吧陪我😭陪陪我好不好';
let passed = 0;

{
  assert.equal(EMOJI_AT_19.indexOf('😭'), 19, 'the emoji straddles the old 20-unit cut');
  const session = { title: '新对话', messages: [{ role: 'user', content: EMOJI_AT_19 }] };
  assert.equal(autoTitle(session), true);
  assert.doesNotMatch(session.title, LONE_SURROGATE);
  assert.equal(session.title, '今天下班好累啊想跟你说说话聊聊天吧陪我😭…');
  passed++;
}

{
  const session = { title: '新对话', messages: [{ role: 'user', content: '😭😭😭' }] };
  autoTitle(session);
  assert.equal(session.title, '😭😭😭', 'short titles stay whole');
  passed++;
}

{
  // Titles already broken by older versions heal when the browser record or a backup is read.
  const broken = `${EMOJI_AT_19.slice(0, 20)}…`;
  assert.match(broken, LONE_SURROGATE);
  const snapshot = normalizeSnapshot({
    sessions: [{ id: 's_old', title: broken, createdAt: 1, updatedAt: 2, settings: {}, messages: [] }],
  }, DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL);
  assert.equal(snapshot.sessions[0].title, '今天下班好累啊想跟你说说话聊聊天吧陪我…');
  assert.doesNotMatch(JSON.stringify(snapshot), /\\ud83d(?!\\ude)/i);
  passed++;
}

console.log(`Session titles: ${passed} scenarios passed`);

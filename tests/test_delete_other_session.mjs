import assert from 'node:assert/strict';
import test from 'node:test';
import { setup } from './helpers/adv_main.mjs';
import { tick } from './helpers/chat_stream.mjs';

test('deleting another session keeps the open session reply and its voice going', async t => {
  let onDelete = null;
  let voiceStops = 0;
  const deleted = [];
  const h = setup(t, {
    fetch: async (url, options = {}) => {
      if (String(url).startsWith('/api/chat/sessions/') && options.method === 'DELETE') {
        deleted.push(String(url));
        return Response.json({ status: 'deleted', success: true });
      }
      return Response.json({ backgrounds: [], characters: [] });
    },
    callbacks: {
      renderSessionList(_el, opts) { onDelete = opts.onDelete; },
      stopCurrentVoice() { voiceStops++; },
    },
  });
  h.dom.sessionList = document.createElement('div');
  h.app.fullRender();
  assert.ok(typeof onDelete === 'function');

  assert.ok(h.app.sendMessage('你好') === true);
  await h.stream.requests[0].onChunk('正在回复');
  assert.ok(h.app.conversation.busy === true);
  voiceStops = 0;

  await onDelete('s_1');
  await tick();
  assert.ok(deleted.length === 1, 'the other session is deleted');
  assert.ok(h.app.conversation.busy === true, 'the open session reply keeps streaming');
  assert.ok(voiceStops === 0, 'the open session voice keeps playing');
});

test('deleting the open session still stops its reply', async t => {
  let onDelete = null;
  const h = setup(t, {
    fetch: async (url, options = {}) => (
      String(url).startsWith('/api/chat/sessions/') && options.method === 'DELETE'
        ? Response.json({ status: 'deleted', success: true })
        : Response.json({ backgrounds: [], characters: [] })),
    callbacks: { renderSessionList(_el, opts) { onDelete = opts.onDelete; } },
  });
  h.dom.sessionList = document.createElement('div');
  h.app.fullRender();
  h.app.sendMessage('你好');
  await h.stream.requests[0].onChunk('正在回复');

  await onDelete('s_0');
  await tick();
  assert.ok(h.app.conversation.busy === false);
});

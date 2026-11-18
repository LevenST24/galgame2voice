import assert from 'node:assert/strict';
import { test } from 'node:test';
import { resolveTurnReplyIdentity } from '../frontend/src/controllers/turn_reply_identity.js';

function turnReply(status, extra = {}) {
  return {
    status,
    session_id: 'session_1',
    user_message_id: 101,
    ...extra,
  };
}

function resolveIdentity(overrides = {}) {
  return resolveTurnReplyIdentity({
    sessionId: 'session_1',
    userMessageId: 101,
    ...overrides,
  });
}

async function resolveAfterRetry(transientStatus, messageId) {
  let attempts = 0;
  const result = await resolveIdentity({
    delayMs: 10,
    request: async () => {
      attempts++;
      if (attempts < 2) return turnReply(transientStatus);
      return turnReply('ready', { message_id: messageId });
    },
  });
  return { attempts, result };
}

test('resolveTurnReplyIdentity: unavailable retries within budget', async () => {
  const { attempts, result } = await resolveAfterRetry('unavailable', 303);

  assert.equal(attempts, 2);
  assert.equal(result.status, 'ready');
  assert.equal(result.messageId, 303);
});

test('resolveTurnReplyIdentity: mismatched session_id or user_message_id is rejected', async () => {
  await assert.rejects(
    resolveIdentity({
      request: async () => turnReply('ready', { session_id: 'wrong_session', message_id: 202 }),
    }),
    /不匹配/
  );

  await assert.rejects(
    resolveIdentity({
      request: async () => turnReply('ready', { user_message_id: 999, message_id: 202 }),
    }),
    /不匹配/
  );
});

test('resolveTurnReplyIdentity: aborted signal immediately rejects with AbortError', async () => {
  const controller = new AbortController();
  controller.abort();

  await assert.rejects(
    resolveIdentity({
      signal: controller.signal,
      request: async () => {
        throw new Error('should not be called');
      },
    }),
    { name: 'AbortError' }
  );
});

test('resolveTurnReplyIdentity: checkCurrent failure aborts and does not claim ID', async () => {
  let valid = true;
  await assert.rejects(
    resolveIdentity({
      checkCurrent: () => {
        if (!valid) throw new DOMException('操作已取消', 'AbortError');
      },
      request: async () => {
        valid = false; // session switched or line edited during fetch
        return turnReply('ready', { message_id: 202 });
      },
    }),
    { name: 'AbortError' }
  );
});

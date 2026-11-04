import assert from 'node:assert/strict';
import { test } from 'node:test';
import { resolveTurnReplyIdentity } from '../frontend/src/controllers/turn_reply_identity.js';

test('resolveTurnReplyIdentity: returns messageId on ready with matching keys', async () => {
  const calls = [];
  const result = await resolveTurnReplyIdentity({
    sessionId: 'session_1',
    userMessageId: 101,
    request: async (url, options) => {
      calls.push({ url, options });
      return {
        status: 'ready',
        session_id: 'session_1',
        user_message_id: 101,
        message_id: 202,
      };
    },
  });

  assert.equal(calls.length, 1);
  assert.equal(result.status, 'ready');
  assert.equal(result.messageId, 202);
});

test('resolveTurnReplyIdentity: pending retries until ready within budget', async () => {
  let attempts = 0;
  const result = await resolveTurnReplyIdentity({
    sessionId: 'session_1',
    userMessageId: 101,
    delayMs: 10,
    request: async () => {
      attempts++;
      if (attempts < 2) {
        return {
          status: 'pending',
          session_id: 'session_1',
          user_message_id: 101,
        };
      }
      return {
        status: 'ready',
        session_id: 'session_1',
        user_message_id: 101,
        message_id: 202,
      };
    },
  });

  assert.equal(attempts, 2);
  assert.equal(result.status, 'ready');
  assert.equal(result.messageId, 202);
});

test('resolveTurnReplyIdentity: pending times out with Chinese message after max 3 attempts', async () => {
  let attempts = 0;
  await assert.rejects(
    resolveTurnReplyIdentity({
      sessionId: 'session_1',
      userMessageId: 101,
      maxAttempts: 3,
      delayMs: 5,
      request: async () => {
        attempts++;
        return {
          status: 'pending',
          session_id: 'session_1',
          user_message_id: 101,
        };
      },
    }),
    /中断回复尚未就绪，原记录已保留，请稍后重试。/
  );

  assert.equal(attempts, 3, 'must retry at most 3 times');
});

test('resolveTurnReplyIdentity: unavailable retries within budget', async () => {
  let attempts = 0;
  const result = await resolveTurnReplyIdentity({
    sessionId: 'session_1',
    userMessageId: 101,
    delayMs: 10,
    request: async () => {
      attempts++;
      if (attempts === 1) {
        return {
          status: 'unavailable',
          session_id: 'session_1',
          user_message_id: 101,
        };
      }
      return {
        status: 'ready',
        session_id: 'session_1',
        user_message_id: 101,
        message_id: 303,
      };
    },
  });

  assert.equal(attempts, 2);
  assert.equal(result.status, 'ready');
  assert.equal(result.messageId, 303);
});

test('resolveTurnReplyIdentity: gone immediately stops without retry', async () => {
  let attempts = 0;
  await assert.rejects(
    resolveTurnReplyIdentity({
      sessionId: 'session_1',
      userMessageId: 101,
      maxAttempts: 3,
      request: async () => {
        attempts++;
        return {
          status: 'gone',
          session_id: 'session_1',
          user_message_id: 101,
        };
      },
    }),
    /已被删除或清空/
  );

  assert.equal(attempts, 1, 'gone must immediately stop');
});

test('resolveTurnReplyIdentity: ambiguous immediately stops without retry', async () => {
  let attempts = 0;
  await assert.rejects(
    resolveTurnReplyIdentity({
      sessionId: 'session_1',
      userMessageId: 101,
      maxAttempts: 3,
      request: async () => {
        attempts++;
        return {
          status: 'ambiguous',
          session_id: 'session_1',
          user_message_id: 101,
        };
      },
    }),
    /多条匹配/
  );

  assert.equal(attempts, 1, 'ambiguous must immediately stop');
});

test('resolveTurnReplyIdentity: mismatched session_id or user_message_id is rejected', async () => {
  await assert.rejects(
    resolveTurnReplyIdentity({
      sessionId: 'session_1',
      userMessageId: 101,
      request: async () => ({
        status: 'ready',
        session_id: 'wrong_session',
        user_message_id: 101,
        message_id: 202,
      }),
    }),
    /不匹配/
  );

  await assert.rejects(
    resolveTurnReplyIdentity({
      sessionId: 'session_1',
      userMessageId: 101,
      request: async () => ({
        status: 'ready',
        session_id: 'session_1',
        user_message_id: 999,
        message_id: 202,
      }),
    }),
    /不匹配/
  );
});

test('resolveTurnReplyIdentity: aborted signal immediately rejects with AbortError', async () => {
  const controller = new AbortController();
  controller.abort();

  await assert.rejects(
    resolveTurnReplyIdentity({
      sessionId: 'session_1',
      userMessageId: 101,
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
    resolveTurnReplyIdentity({
      sessionId: 'session_1',
      userMessageId: 101,
      checkCurrent: () => {
        if (!valid) throw new DOMException('操作已取消', 'AbortError');
      },
      request: async () => {
        valid = false; // session switched or line edited during fetch
        return {
          status: 'ready',
          session_id: 'session_1',
          user_message_id: 101,
          message_id: 202,
        };
      },
    }),
    { name: 'AbortError' }
  );
});

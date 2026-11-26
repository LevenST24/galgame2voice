let nextStatus = () => 200;
import assert from 'node:assert/strict';
import test from 'node:test';

const stored = new Map();
const sent = [];
globalThis.location = { origin: 'http://127.0.0.1:8000' };
globalThis.window = {
  sessionStorage: {
    getItem: (k) => (stored.has(k) ? stored.get(k) : null),
    setItem: (k, v) => stored.set(k, String(v)),
    removeItem: (k) => stored.delete(k),
  },
  prompt: () => null,
  alert: () => {},
  fetch: async (_input, init = {}) => {
    sent.push(init.headers?.get?.('Authorization') ?? null);
    const status = nextStatus();
    return { status, ok: status < 400 };
  },
};
globalThis.document = { getElementById: () => null };

const { normalizeConsoleToken, isUsableConsoleToken, getConsoleToken, initAuthInterceptor } = await import('../frontend/src/auth.js');
initAuthInterceptor();

test('a token typed with a Chinese IME or pasted with invisible characters is cleaned up', () => {
  assert.equal(normalizeConsoleToken('ａｂｃ１２３'), 'abc123');
  assert.equal(normalizeConsoleToken('​abc-123﻿ '), 'abc-123');
  assert.equal(isUsableConsoleToken('abc-123_~'), true);
  assert.equal(isUsableConsoleToken('café'), false);
  assert.equal(isUsableConsoleToken('令牌'), false);
});

test('a stored full-width token is sent in its ASCII form', async () => {
  stored.set('galgame_console_token', 'ａｂｃ１２３');
  assert.equal(getConsoleToken(), 'abc123');
  sent.length = 0;
  await window.fetch('/api/config');
  assert.deepEqual(sent, ['Bearer abc123']);
});

test('a stored token that can never match is left off instead of breaking every request', async () => {
  stored.set('galgame_console_token', '令牌abc');
  sent.length = 0;
  // Before the fix, Headers.set threw "Cannot convert argument to a ByteString" here.
  await window.fetch('/api/config');
  assert.deepEqual(sent, [null]);
});

test('retrying 401 with a wrong token clears it, reprompts with reason, and stops on cancel', async () => {
  stored.clear();
  const prompts = [];
  window.prompt = (msg) => (prompts.push(msg), prompts.length === 1 ? 'wrong-tok' : null);
  nextStatus = () => 401;
  try {
    const res = await window.fetch('/api/config');
    assert.equal(res.status, 401);
    assert.equal(prompts.length, 2);
    assert.match(prompts[1], /令牌不对.*data\\\.console_token/);
    assert.equal(getConsoleToken(), '');
  } finally { nextStatus = () => 200; window.prompt = () => null; }
});

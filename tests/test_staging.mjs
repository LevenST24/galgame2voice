import test from 'node:test';
import assert from 'node:assert/strict';
import { createStaging, stagingTransform } from '../frontend/src/adv/staging.js';

test('explicit directions and limits', () => {
  const s = createStaging({ random: () => 1 });
  assert.deepEqual(s.next({ move: 'left' }), { x: -1, depth: 1 });
  for (let i = 0; i < 4; i++) s.next({ move: 'left' });
  assert.equal(s.current().x, -2);
  for (let i = 0; i < 8; i++) s.next({ move: 'right' });
  assert.equal(s.current().x, 2);
  assert.equal(s.next({ move: 'center' }).x, 0);
  assert.equal(s.next({ move: 'approach' }).depth, 2);
  assert.equal(s.next({ move: 'closeup' }).depth, 3);
  for (let i = 0; i < 5; i++) s.next({ move: 'retreat' });
  assert.equal(s.current().depth, 0);
  assert.deepEqual(s.next({ enabled: false, move: 'right' }), { x: 0, depth: 1 });
});
test('automatic direction respects interval and returns to normal', () => {
  const s = createStaging({ random: () => 0 });
  s.next({ move: 'closeup' });
  assert.equal(s.next({ emotion: 'sad' }).depth, 3);
  assert.equal(s.next({ emotion: 'sad' }).depth, 3);
  assert.equal(s.next({ emotion: 'sad' }).depth, 2);
  const idle = createStaging({ random: () => 1 });
  idle.next({ move: 'closeup' });
  for (let i = 0; i < 7; i++) idle.next({});
  assert.equal(idle.current().depth, 2);
  for (let i = 0; i < 7; i++) idle.next({});
  assert.equal(idle.current().depth, 1);
});
test('face and head remain within the safe area', () => {
  for (let depth = 0; depth < 4; depth++) for (let x = -2; x <= 2; x++) {
    const t = stagingTransform({ x, depth }, { centerX: 1100, headY: 70, faceBottom: 500 });
    assert.ok(t.y + 70 * t.scale >= 20);
    assert.ok(t.y + 500 * t.scale <= 680);
    assert.ok(t.x + 1100 * t.scale >= 220 && t.x + 1100 * t.scale <= 1700);
  }
});

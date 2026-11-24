import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('../frontend/src/adv/adv.css', import.meta.url), 'utf8');

function declarations(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const block = css.match(new RegExp(`(?:^|\\n)${escaped} \\{([^}]*)\\}`));
  assert.ok(block, `missing rule ${selector}`);
  const out = {};
  for (const part of block[1].split(';')) {
    const index = part.indexOf(':');
    if (index > 0) out[part.slice(0, index).trim()] = part.slice(index + 1).trim();
  }
  return out;
}

const TYPE_KEYS = ['font-size', 'line-height', 'font-weight', 'letter-spacing', 'white-space'];

for (const [measurer, row] of [
  ['.adv-text-measurer', '.adv-primary-text'],
  ['.adv-subtitle-measurer', '.adv-subtitle-text'],
]) {
  test(`${measurer} measures text the way ${row} draws it`, () => {
    const measured = declarations(measurer);
    const drawn = declarations(row);
    for (const key of TYPE_KEYS) {
      assert.equal(measured[key], drawn[key], `${measurer} ${key} differs from ${row}`);
    }
  });
}

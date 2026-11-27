import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const stylesCss = readFileSync(new URL('../frontend/src/styles.css', import.meta.url), 'utf8');
const appCss = readFileSync(new URL('../frontend/src/adv/app.css', import.meta.url), 'utf8');
const interfaceCss = readFileSync(new URL('../frontend/src/interface.css', import.meta.url), 'utf8');

function resolveDeclarations(sheets, matcher) {
  const out = {};
  for (const css of sheets) {
    for (const [, raw, body] of css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}@]+)\{([^{}]*)\}/g)) {
      if (!raw.split(',').some(sel => matcher(sel.trim()))) continue;
      for (const part of body.split(';')) {
        const i = part.indexOf(':');
        if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
      }
    }
  }
  return out;
}

test('session drawer keeps a positive min-height on .session-list inside narrow/short stages', () => {
  const sheets = [stylesCss, appCss, interfaceCss];
  const sidebar = resolveDeclarations(sheets, sel => sel.endsWith('.adv-drawer .sidebar'));
  const list = resolveDeclarations(sheets, sel => sel === '.session-list' || sel.endsWith('.adv-drawer .session-list'));
  assert.equal(sidebar['overflow-y'], 'auto');
  const minHeightPx = /^(\d+(?:\.\d+)?)px$/.exec(list['min-height'] || '');
  assert.ok(
    minHeightPx && Number(minHeightPx[1]) >= 80,
    `expected .adv-drawer .session-list min-height >= 80px to avoid 0px flex collapse on 640x720/375x667, got "${list['min-height'] || ''}"`,
  );
});

test('session drawer inset scales down on short portrait stages instead of locking 48px vertical chrome', () => {
  const drawer = resolveDeclarations([appCss, interfaceCss], sel => sel.endsWith('.adv-drawer'));
  assert.match(drawer.inset || '', /min\(\s*24px\s*,/);
});

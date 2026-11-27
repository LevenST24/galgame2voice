import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const advCss = readFileSync(new URL('../frontend/src/adv/adv.css', import.meta.url), 'utf8');
const interfaceCss = readFileSync(new URL('../frontend/src/interface.css', import.meta.url), 'utf8');

function decls(selector) {
  const out = {};
  for (const css of [advCss, interfaceCss]) {
    for (const [, raw, body] of css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}@]+)\{([^{}]*)\}/g)) {
      if (!raw.split(',').some(s => { const c = s.trim().replace(/:not\([^)]*\)/g, ''); return !c.includes('.is-home') && c.split(/[\s>+~]+/).at(-1) === selector; })) continue;
      for (const part of body.split(';')) { const i = part.indexOf(':'); if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim(); }
    }
  }
  return out;
}

test('quick menu stays inside stage height and scrolls horizontally on narrow viewports', () => {
  const strip = decls('.adv-quick-menu-strip');
  const menu = decls('.adv-quick-menu');
  const track = decls('.adv-qm-track');
  assert.match(strip.height || '', /^max\(/);
  assert.match(menu.height || '', /^max\(/);
  assert.equal(menu.left, '0');
  assert.equal(menu['overflow-x'], 'auto');
  assert.equal(track['margin-left'], 'auto');
});

test('quick menu buttons stay single-line and fit within strip height on narrow viewports', () => {
  const btn = decls('.adv-qm-btn');
  const status = decls('.adv-qm-status');
  const strip = decls('.adv-quick-menu-strip');
  assert.equal(btn['white-space'], 'nowrap');
  assert.equal(btn.flex, 'none');
  const statusMin = /^max\(\s*(\d+(?:\.\d+)?)px/.exec(status['min-height'] || '');
  const stripMin = /^max\(\s*(\d+(?:\.\d+)?)px/.exec(strip.height || '');
  assert.ok(statusMin && stripMin && Number(statusMin[1]) <= Number(stripMin[1]));
});

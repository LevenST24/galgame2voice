import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const advCss = readFileSync(new URL('../frontend/src/adv/adv.css', import.meta.url), 'utf8');
const interfaceCss = readFileSync(new URL('../frontend/src/interface.css', import.meta.url), 'utf8');

function expandSelectors(raw) {
  const m = raw.match(/:is\(([^()]*)\)/);
  if (!m) return raw.split(',').map((s) => s.trim());
  const head = raw.slice(0, m.index), tail = raw.slice(m.index + m[0].length);
  return m[1].split(',').flatMap((part) => expandSelectors(`${head}${part.trim()}${tail}`));
}

function declarations(sheets, selector) {
  const out = {};
  for (const css of sheets) {
    for (const [, raw, body] of css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}@]+)\{([^{}]*)\}/g)) {
      if (!raw.includes(selector)) continue;
      if (!expandSelectors(raw).some((s) => !s.replace(/:not\([^)]*\)/g, '').includes('.is-home') && s.split(/[\s>+~]+/).at(-1) === selector)) continue;
      for (const part of body.split(';')) {
        const i = part.indexOf(':');
        if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
      }
    }
  }
  return out;
}

const BASE_KEYS = ['font-size', 'line-height', 'font-weight', 'letter-spacing', 'white-space'];
for (const [label, sheets, keys] of [['adv.css', [advCss], BASE_KEYS], ['adv.css + interface.css', [advCss, interfaceCss], ['font-family', ...BASE_KEYS]]]) {
  for (const [measurer, row] of [['.adv-text-measurer', '.adv-primary-text'], ['.adv-subtitle-measurer', '.adv-subtitle-text']]) {
    test(`${label}: ${measurer} measures text the way ${row} draws it`, () => {
      const measured = declarations(sheets, measurer), drawn = declarations(sheets, row);
      for (const key of keys) {
        assert.ok(drawn[key], `${label}: missing ${key} on ${row}`);
        assert.equal(measured[key], drawn[key], `${label}: ${measurer} ${key} differs from ${row}`);
      }
    });
  }
}

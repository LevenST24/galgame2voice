import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const FRONTEND_DIR = fileURLToPath(new URL('../frontend', import.meta.url));
const JSCONFIG_PATH = path.join(FRONTEND_DIR, 'jsconfig.json');
const PACKAGE_JSON_PATH = path.join(FRONTEND_DIR, 'package.json');

const EXPECTED_CORE_FILES = [
  'src/audio_request.js',
  'src/request.js',
  'src/session_data.js',
  'src/store.js',
  'src/cache.js',
  'src/controllers/turn_reply_identity.js',
];

test('frontend/jsconfig.json enables checkJs + noEmit and covers all 6 core modules with // @ts-check', () => {
  assert.equal(fs.existsSync(JSCONFIG_PATH), true, 'frontend/jsconfig.json must exist');
  const jsconfig = JSON.parse(fs.readFileSync(JSCONFIG_PATH, 'utf8'));

  assert.equal(jsconfig?.compilerOptions?.checkJs, true, 'compilerOptions.checkJs must be true');
  assert.equal(jsconfig?.compilerOptions?.noEmit, true, 'compilerOptions.noEmit must be true');
  assert.equal(jsconfig?.compilerOptions?.strictNullChecks, true, 'compilerOptions.strictNullChecks must be true');
  assert.equal(Array.isArray(jsconfig?.include), true, 'jsconfig.include must be an array');

  for (const relPath of EXPECTED_CORE_FILES) {
    assert.equal(
      jsconfig.include.includes(relPath),
      true,
      `jsconfig.include must contain ${relPath}`,
    );
    const fullPath = path.join(FRONTEND_DIR, relPath);
    assert.equal(fs.existsSync(fullPath), true, `${relPath} must exist on disk`);
    const content = fs.readFileSync(fullPath, 'utf8');
    const head = content.split(/\r?\n/, 5).join('\n');
    assert.match(head, /^\/\/ @ts-check\b/m, `${relPath} must start with // @ts-check`);
  }

  const pkg = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, 'utf8'));
  assert.equal(pkg?.scripts?.typecheck, 'tsc -p jsconfig.json');
  assert.equal(pkg?.devDependencies?.typescript, '5.9.3');
});

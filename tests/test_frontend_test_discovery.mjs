import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import {
  EXCLUDED_TESTS,
  TEMP_TEST_PREFIX_RE,
  discoverFrontendTests,
  runFrontendTests,
} from '../frontend/run_tests.mjs';

const PACKAGE_JSON_PATH = fileURLToPath(new URL('../frontend/package.json', import.meta.url));

test('frontend/package.json scripts.test uses run_tests.mjs instead of hardcoded test list', () => {
  const pkg = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, 'utf8'));
  assert.equal(pkg.scripts.test, 'node run_tests.mjs');
  assert.equal(pkg.scripts.test.includes('../tests/test_'), false);
  assert.equal(Object.isFrozen(EXCLUDED_TESTS), true);
  assert.equal(TEMP_TEST_PREFIX_RE instanceof RegExp, true);
});

test('discoverFrontendTests discovers top-level test files in ASCII order and excludes helpers and temp files', () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'g2v-test-disc-'));
  try {
    const validFiles = ['test_b.mjs', 'test_a.js', 'test_c.mjs'];
    const ignoredFiles = [
      'helper.mjs',
      'README.md',
      'test_zz_temp.mjs',
      '_probe_foo.mjs',
      'temp_bar.mjs',
      'debug_baz.js',
      'scratch_qux.mjs',
    ];
    for (const name of [...validFiles, ...ignoredFiles]) {
      fs.writeFileSync(path.join(tmpDir, name), '// stub\n', 'utf8');
    }
    const helpersDir = path.join(tmpDir, 'helpers');
    fs.mkdirSync(helpersDir, { recursive: true });
    fs.writeFileSync(path.join(helpersDir, 'test_nested.mjs'), '// nested stub\n', 'utf8');

    assert.deepEqual(discoverFrontendTests(tmpDir), ['test_a.js', 'test_b.mjs', 'test_c.mjs']);
    assert.deepEqual(
      discoverFrontendTests(tmpDir, { excluded: { 'test_b.mjs': 'reason' } }),
      ['test_a.js', 'test_c.mjs'],
    );
    assert.deepEqual(
      discoverFrontendTests(tmpDir, { excluded: new Set(['test_a.js']) }),
      ['test_b.mjs', 'test_c.mjs'],
    );
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test('runFrontendTests propagates child process exit code and short-circuits on failure', () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'g2v-test-disc-'));
  try {
    for (const name of ['test_b.mjs', 'test_a.js', 'test_c.mjs']) {
      fs.writeFileSync(path.join(tmpDir, name), '// stub\n', 'utf8');
    }

    const invokedFail = [];
    const errors = [];
    const failResult = runFrontendTests({
      testsDir: tmpDir,
      spawn: (cmd, args) => {
        const base = path.basename(args[0]);
        invokedFail.push({ cmd, base });
        if (base === 'test_b.mjs') {
          return { status: 1, signal: null, error: null };
        }
        return { status: 0, signal: null, error: null };
      },
      log: () => {},
      errorLog: (msg) => errors.push(msg),
    });

    assert.deepEqual(failResult, {
      ok: false,
      total: 3,
      passed: 1,
      failedFile: 'test_b.mjs',
      exitCode: 1,
    });
    assert.deepEqual(
      invokedFail.map((item) => item.base),
      ['test_a.js', 'test_b.mjs'],
    );
    assert.equal(
      errors.some((msg) => msg.includes('[frontend-test] FAILED (2/3): test_b.mjs (exit 1)')),
      true,
    );

    const invokedPass = [];
    const logs = [];
    const passResult = runFrontendTests({
      testsDir: tmpDir,
      spawn: (_cmd, args) => {
        invokedPass.push(path.basename(args[0]));
        return { status: 0, signal: null, error: null };
      },
      log: (msg) => logs.push(msg),
      errorLog: () => {},
    });

    assert.deepEqual(passResult, {
      ok: true,
      total: 3,
      passed: 3,
      failedFile: null,
      exitCode: 0,
    });
    assert.deepEqual(invokedPass, ['test_a.js', 'test_b.mjs', 'test_c.mjs']);
    assert.equal(
      logs.some((msg) => msg.includes('[frontend-test] PASSED 3/3 test files.')),
      true,
    );
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test('discoverFrontendTests scans real tests/ directory and includes core tests without helpers', () => {
  const files = discoverFrontendTests();
  assert.equal(files.length >= 62, true, `expected >= 62 test files, got ${files.length}`);
  assert.equal(files.includes('test_m7_audio_lru_challenger2.js'), true);
  assert.equal(files.includes('test_adv_main.mjs'), true);
  assert.equal(files.includes('test_frontend_test_discovery.mjs'), true);
  assert.equal(files.includes('adv_dom.mjs'), false);
  assert.equal(files.includes('turn_reply_roundtrip.mjs'), false);
  for (const name of files) {
    assert.match(name, /^test_.*\.(?:mjs|js)$/);
    assert.equal(TEMP_TEST_PREFIX_RE.test(name), false);
  }
  const sorted = [...files].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  assert.deepEqual(files, sorted);
});

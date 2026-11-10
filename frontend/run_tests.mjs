import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const DEFAULT_TESTS_DIR = fileURLToPath(new URL('../tests', import.meta.url));
const TEST_FILE_RE = /^test_.*\.(?:mjs|js)$/;

// Top-level tests/ files to explicitly exclude (filename -> reason).
// Subdirectories such as tests/helpers/*.mjs are naturally excluded because
// discoverFrontendTests only inspects regular files in the top-level testsDir.
export const EXCLUDED_TESTS = Object.freeze({});

export const TEMP_TEST_PREFIX_RE = /^(?:test_zz_|_probe_|temp_|debug_|scratch_)/;

function isExcluded(excluded, name) {
  if (!excluded) return false;
  if (excluded instanceof Set) return excluded.has(name);
  return Object.hasOwn(excluded, name);
}

export function discoverFrontendTests(
  testsDir = DEFAULT_TESTS_DIR,
  { excluded = EXCLUDED_TESTS } = {},
) {
  const entries = fs.readdirSync(testsDir, { withFileTypes: true });
  const names = [];
  for (const entry of entries) {
    if (!entry.isFile()) continue;
    if (!TEST_FILE_RE.test(entry.name)) continue;
    if (TEMP_TEST_PREFIX_RE.test(entry.name)) continue;
    if (isExcluded(excluded, entry.name)) continue;
    names.push(entry.name);
  }
  names.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  return names;
}

export function runFrontendTests({
  testsDir = DEFAULT_TESTS_DIR,
  excluded = EXCLUDED_TESTS,
  spawn = spawnSync,
  env = process.env,
  log = console.log,
  errorLog = console.error,
} = {}) {
  const files = discoverFrontendTests(testsDir, { excluded });
  if (files.length === 0) {
    errorLog(`[frontend-test] No test files discovered in ${testsDir}`);
    return {
      ok: false,
      total: 0,
      passed: 0,
      failedFile: null,
      exitCode: 1,
    };
  }

  let passed = 0;
  for (const name of files) {
    const fullPath = path.join(testsDir, name);
    const res = spawn(process.execPath, [fullPath], { stdio: 'inherit', env });
    if (res.error || res.signal || res.status !== 0) {
      const exitCode = typeof res.status === 'number' && res.status !== 0 ? res.status : 1;
      errorLog(`[frontend-test] FAILED (${passed + 1}/${files.length}): ${name} (exit ${exitCode})`);
      return {
        ok: false,
        total: files.length,
        passed,
        failedFile: name,
        exitCode,
      };
    }
    passed += 1;
  }

  log(`[frontend-test] PASSED ${passed}/${files.length} test files.`);
  return {
    ok: true,
    total: files.length,
    passed,
    failedFile: null,
    exitCode: 0,
  };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const result = runFrontendTests();
  process.exit(result.exitCode);
}

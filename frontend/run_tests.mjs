import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const DEFAULT_TESTS_DIR = fileURLToPath(new URL('../tests', import.meta.url));
const TEST_FILE_RE = /^test_.*\.(?:mjs|js)$/;

export const DEFAULT_CONCURRENCY = 4;

export function resolveTestConcurrency(value = process.env.G2V_TEST_CONCURRENCY) {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  if (!Number.isFinite(parsed) || parsed < 1) return DEFAULT_CONCURRENCY;
  return Math.min(8, parsed);
}

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

function runTestFileAsync(fullPath, spawnAsync, activeChildren) {
  return new Promise((resolve) => {
    const stdout = [];
    const stderr = [];
    let settled = false;
    const child = spawnAsync(process.execPath, [fullPath], {
      stdio: ['ignore', 'pipe', 'pipe'],
      env: process.env,
    });
    const finish = (code, signal, error) => {
      if (settled) return;
      settled = true;
      activeChildren.delete(child);
      resolve({
        code,
        signal,
        error,
        stdout: Buffer.concat(stdout).toString('utf8'),
        stderr: Buffer.concat(stderr).toString('utf8'),
      });
    };
    activeChildren.add(child);
    child.stdout.on('data', (c) => stdout.push(Buffer.from(c)));
    child.stderr.on('data', (c) => stderr.push(Buffer.from(c)));
    child.on('error', (error) => finish(1, null, error));
    child.on('close', (code, signal) => finish(code, signal, null));
  });
}

export async function runFrontendTestsParallel({
  testsDir = DEFAULT_TESTS_DIR,
  excluded = EXCLUDED_TESTS,
  concurrency,
  spawnAsync = spawn,
  log = console.log,
  errorLog = console.error,
  writeStdout = (text) => process.stdout.write(text),
  writeStderr = (text) => process.stderr.write(text),
} = {}) {
  const files = discoverFrontendTests(testsDir, { excluded });
  if (files.length === 0) {
    errorLog(`[frontend-test] No test files discovered in ${testsDir}`);
    return { ok: false, total: 0, passed: 0, failedFile: null, exitCode: 1 };
  }

  const activeChildren = new Set();
  const workerCount = Math.min(resolveTestConcurrency(concurrency), files.length);
  let nextIndex = 0;
  let passed = 0;
  let firstFailure = null;

  async function worker() {
    while (!firstFailure) {
      const index = nextIndex++;
      if (index >= files.length) return;
      const name = files[index];
      const res = await runTestFileAsync(path.join(testsDir, name), spawnAsync, activeChildren);
      if (firstFailure) return;
      if (!res.error && !res.signal && res.code === 0) {
        passed += 1;
        log(`[frontend-test] (${index + 1}/${files.length}) ${name}`);
        continue;
      }
      const exitCode = typeof res.code === 'number' && res.code !== 0 ? res.code : 1;
      firstFailure = { ok: false, total: files.length, passed, failedFile: name, exitCode };
      for (const child of activeChildren) {
        child.kill();
      }
      if (res.stdout) writeStdout(res.stdout);
      if (res.stderr) writeStderr(res.stderr);
      errorLog(`[frontend-test] FAILED (${index + 1}/${files.length}): ${name} (exit ${exitCode})`);
      return;
    }
  }

  await Promise.all(Array.from({ length: workerCount }, worker));
  if (firstFailure) return firstFailure;
  log(`[frontend-test] All ${passed} test files passed.`);
  return { ok: true, total: files.length, passed, failedFile: null, exitCode: 0 };
}

if (process.argv[1] && path.resolve(process.argv[1]) === __filename) {
  const summary = await runFrontendTestsParallel();
  if (!summary.ok) process.exit(summary.exitCode);
}

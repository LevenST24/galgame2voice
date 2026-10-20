import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, symlink, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname, resolve } from 'node:path';
import http from 'node:http';
import { once } from 'node:events';

async function setup(t) {
  const { createPackServer } = await import('../frontend/dev/serve-pack.mjs');
  const temporary = await mkdtemp(join(tmpdir(), 'sprite-pack-'));
  const root = join(temporary, 'pack');
  await mkdir(root);
  await writeFile(join(root, 'expressions.json'), '{"version":2}');
  await writeFile(join(root, 'face#1.webp'), 'image');
  await writeFile(join(temporary, 'private.json'), 'outside');
  const server = await createPackServer(root);
  t.after(async () => {
    await new Promise(resolve => server.close(resolve));
    assert.equal(dirname(resolve(temporary)), resolve(tmpdir()), 'cleanup stays inside the named temporary root');
    await rm(temporary, { recursive: true, force: true });
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const request = (path, { method = 'GET', origin } = {}) => new Promise((resolve, reject) => {
    const req = http.request({ hostname: '127.0.0.1', port: server.address().port, path, method,
      headers: origin ? { Origin: origin } : {} }, res => {
      let body = ''; res.setEncoding('utf8');
      res.on('data', chunk => { body += chunk; });
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body }));
    });
    req.on('error', reject); req.end();
  });
  return { root, temporary, request };
}

test('pack server provides GET/HEAD with loopback HTTP CORS only', async t => {
  const h = await setup(t);
  for (const origin of ['http://localhost:3015', 'http://127.0.0.1:3015']) {
    const response = await h.request('/expressions.json', { origin });
    assert.equal(response.status, 200);
    assert.equal(response.headers['access-control-allow-origin'], origin);
    assert.equal(response.headers.vary, 'Origin');
    assert.match(response.headers['content-type'], /application\/json/);
    assert.equal(response.body, '{"version":2}');
  }
  const head = await h.request('/expressions.json', { method: 'HEAD' });
  assert.equal(head.status, 200); assert.equal(head.body, '');
  for (const origin of ['https://localhost:3015', 'http://localhost.evil:3015', 'http://example.com', 'null']) {
    const response = await h.request('/expressions.json', { origin });
    assert.equal(response.status, 403);
    assert.equal(response.headers['access-control-allow-origin'], undefined);
  }
});

test('pack server refuses writes, traversal and encoded separators, and serves encoded filenames', async t => {
  const h = await setup(t);
  for (const method of ['POST', 'PUT', 'DELETE', 'OPTIONS']) {
    const response = await h.request('/expressions.json', { method });
    assert.equal(response.status, 405); assert.equal(response.headers.allow, 'GET, HEAD');
  }
  for (const path of ['/../private.json', '/%2e%2e/private.json', '/..%5cprivate.json', '/%2e%2e%2fprivate.json']) {
    const response = await h.request(path);
    assert.equal(response.status, 403, path); assert.doesNotMatch(response.body, /outside/);
  }
  assert.equal((await h.request('/face%231.webp')).body, 'image');
  assert.equal((await h.request('/')).status, 404);
});

test('pack server refuses symlink or junction escape after realpath resolution', async t => {
  const h = await setup(t);
  const outside = join(h.temporary, 'outside');
  await mkdir(outside); await writeFile(join(outside, 'secret.json'), 'outside');
  await symlink(outside, join(h.root, 'escape'), 'junction');
  const response = await h.request('/escape/secret.json');
  assert.equal(response.status, 403); assert.doesNotMatch(response.body, /outside/);
});

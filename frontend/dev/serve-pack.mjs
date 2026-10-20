/** Dev-only, read-only pack server. Run from the repository root:
 * node frontend/dev/serve-pack.mjs "E:/external/portrait" 18766
 * Then use ?pack=http://127.0.0.1:18766 in sprite-preview.html.
 */
import http from 'node:http';
import { realpath, stat } from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { resolve, relative, isAbsolute, sep, extname } from 'node:path';
import { pathToFileURL } from 'node:url';
import { pipeline } from 'node:stream/promises';

const TYPES = { '.json': 'application/json; charset=utf-8', '.webp': 'image/webp',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.svg': 'image/svg+xml', '.avif': 'image/avif', '.gif': 'image/gif' };
const localOrigin = /^http:\/\/(?:localhost|127\.0\.0\.1)(?::[0-9]+)?$/;
function inside(root, target) {
  const rel = relative(root, target);
  return rel !== '..' && !rel.startsWith('..' + sep) && !isAbsolute(rel);
}

export async function createPackServer(directory) {
  const root = await realpath(resolve(directory));
  if (!(await stat(root)).isDirectory()) throw new Error('Pack root must be a directory');
  return http.createServer(async (req, res) => {
    const reply = (code, text) => { res.statusCode = code; res.end(req.method === 'HEAD' ? undefined : text); };
    res.setHeader('Vary', 'Origin');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    if (!['GET', 'HEAD'].includes(req.method)) {
      res.setHeader('Allow', 'GET, HEAD'); reply(405, 'Read only'); return;
    }
    const origin = req.headers.origin;
    if (origin && !localOrigin.test(origin)) { reply(403, 'Origin refused'); return; }
    if (origin) res.setHeader('Access-Control-Allow-Origin', origin);
    try {
      // Inspect raw segments before URL normalization can erase parent paths.
      const rawPath = req.url.split('?')[0];
      if (!rawPath.startsWith('/')) { reply(403, 'Path refused'); return; }
      const segments = rawPath.slice(1).split('/').map(segment => decodeURIComponent(segment));
      if (segments.some(segment => segment === '..' || /[/\\:\0]/.test(segment))) {
        reply(403, 'Path refused'); return;
      }
      const target = resolve(root, ...segments);
      if (!inside(root, target)) { reply(403, 'Path refused'); return; }
      const canonical = await realpath(target);
      if (!inside(root, canonical)) { reply(403, 'Path refused'); return; }
      const info = await stat(canonical);
      if (!info.isFile()) { reply(404, 'Not found'); return; }
      res.setHeader('Content-Type', TYPES[extname(canonical).toLowerCase()] || 'application/octet-stream');
      res.setHeader('Content-Length', info.size);
      res.setHeader('Cache-Control', 'no-cache');
      if (req.method === 'HEAD') res.end();
      else await pipeline(createReadStream(canonical), res);
    } catch (error) {
      if (res.headersSent || res.destroyed) { res.destroy(); return; }
      if (error instanceof URIError) reply(400, 'Invalid path encoding');
      else if (['ENOENT', 'ENOTDIR'].includes(error.code)) reply(404, 'Not found');
      else reply(403, 'File unavailable');
    }
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const [, , directory, portText = '18766'] = process.argv;
  const port = Number(portText);
  if (!directory || !Number.isInteger(port) || port < 1 || port > 65535) {
    console.error('Usage: node frontend/dev/serve-pack.mjs "external-pack-directory" [18766]');
    process.exitCode = 1;
  } else {
    try {
      const server = await createPackServer(directory);
      server.on('error', error => { console.error(error.message); process.exitCode = 1; });
      server.listen(port, '127.0.0.1', () => console.log(`Pack: http://127.0.0.1:${port}/expressions.json`));
    } catch (error) { console.error(error.message); process.exitCode = 1; }
  }
}

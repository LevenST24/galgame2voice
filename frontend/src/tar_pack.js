// POSIX.1-1988 ustar tar packer for streaming folder uploads as a Blob without reading file contents into RAM.

const UTF8 = new TextEncoder();
const BLOCK_SIZE = 512;
const MAX_FILE_BYTES = 0o77777777777; // ~8 GiB - 1 (11 octal digits)
const ZERO_BLOCK = new Uint8Array(BLOCK_SIZE);
const EOF_TRAILER = new Uint8Array(BLOCK_SIZE * 2);

function normalizeEntryPath(rawPath) {
  const raw = String(rawPath || '').replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
  if (!raw) throw new Error('角色包内存在空路径文件，无法打包导入。');
  if (raw.includes('\0')) throw new Error(`角色包路径包含非法字符：${raw}`);
  const segs = raw.split('/').filter(Boolean);
  if (!segs.length || segs.some((s) => s === '.' || s === '..')) {
    throw new Error(`角色包路径不合法：${raw}`);
  }
  return segs.join('/');
}

function splitUstarPath(path) {
  const full = UTF8.encode(path);
  if (full.length <= 100) return { nameBytes: full, prefixBytes: new Uint8Array(0) };
  for (let idx = path.lastIndexOf('/'); idx > 0; idx = path.lastIndexOf('/', idx - 1)) {
    const nameStr = path.slice(idx + 1);
    if (!nameStr) continue;
    const nameBytes = UTF8.encode(nameStr);
    const prefixBytes = UTF8.encode(path.slice(0, idx));
    if (nameBytes.length <= 100 && prefixBytes.length <= 155) return { nameBytes, prefixBytes };
  }
  throw new Error(`角色包内文件路径过长，请缩短文件夹或文件名称后重试：${path}`);
}

function writeOctal(header, offset, length, value) {
  const digits = length - 1;
  const octal = Math.max(0, Math.floor(value)).toString(8).padStart(digits, '0');
  for (let i = 0; i < digits; i++) header[offset + i] = octal.charCodeAt(i);
  header[offset + digits] = 0;
}

function buildHeader(path, byteSize, mtimeSec) {
  if (!Number.isFinite(byteSize) || byteSize < 0 || byteSize > MAX_FILE_BYTES) {
    throw new Error(`角色包内单个文件大小超出 8 GiB 上限：${path}`);
  }
  const { nameBytes, prefixBytes } = splitUstarPath(path);
  const h = new Uint8Array(BLOCK_SIZE);
  h.set(nameBytes, 0);
  writeOctal(h, 100, 8, 0o644);
  writeOctal(h, 108, 8, 0);
  writeOctal(h, 116, 8, 0);
  writeOctal(h, 124, 12, byteSize);
  writeOctal(h, 136, 12, Math.min(MAX_FILE_BYTES, Math.max(0, mtimeSec)));
  h.fill(0x20, 148, 156);
  h[156] = 0x30; // regular file '0'
  h.set([0x75, 0x73, 0x74, 0x61, 0x72, 0x00, 0x30, 0x30], 257); // "ustar\0" + "00"
  writeOctal(h, 329, 8, 0);
  writeOctal(h, 337, 8, 0);
  if (prefixBytes.length > 0) h.set(prefixBytes, 345);

  let sum = 0;
  for (let i = 0; i < BLOCK_SIZE; i++) sum += h[i];
  const chk = sum.toString(8).padStart(6, '0');
  for (let i = 0; i < 6; i++) h[148 + i] = chk.charCodeAt(i);
  h[154] = 0x00;
  h[155] = 0x20;
  return h;
}

/**
 * Packs `{ path, file }` entries into a ustar `.tar` Blob without reading file contents into RAM.
 * @param {Array<{ path: string, file: Blob & { lastModified?: number } }>} entries
 * @returns {Blob}
 */
export function packTarBlob(entries) {
  /** @type {BlobPart[]} */
  const parts = [];
  const defaultMtime = Math.floor(Date.now() / 1000);
  for (const entry of entries || []) {
    if (!entry?.file) continue;
    const path = normalizeEntryPath(entry.path);
    const size = Number(entry.file.size ?? 0);
    const mtime = typeof entry.file.lastModified === 'number' ? Math.floor(entry.file.lastModified / 1000) : defaultMtime;
    parts.push(buildHeader(path, size, mtime), entry.file);
    const rem = size % BLOCK_SIZE;
    if (rem !== 0) parts.push(ZERO_BLOCK.subarray(0, BLOCK_SIZE - rem));
  }
  parts.push(EOF_TRAILER);
  return new Blob(parts, { type: 'application/x-tar' });
}

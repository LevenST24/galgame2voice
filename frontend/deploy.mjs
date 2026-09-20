// 构建产物部署：dist → 后端 static 目录
// 清理旧 assets 与冗余文件，消除构建产物双源漂移，同时严格保护立绘资源与关键目录
import { cpSync, rmSync, readdirSync, statSync, existsSync, realpathSync } from 'node:fs';
import { join, dirname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const dist = join(here, 'dist');
const target = join(here, '..', 'galgame2voice', 'static');
const projectRoot = join(here, '..');

if (!existsSync(dist) || !statSync(dist).isDirectory()) {
  console.error('[deploy] 错误：dist 目录不存在，请先执行 npm run build');
  process.exit(1);
}

// 部署目标边界校验：必须存在且 realpath 解析后位于项目根目录内，防止 rm -rf 误伤其他目录
if (!existsSync(target)) {
  console.error(`[deploy] 错误：部署目标不存在: ${resolve(target)}`);
  process.exit(1);
}
const realTarget = realpathSync(target);
const realRoot = realpathSync(projectRoot);
if (realTarget !== realRoot && !realTarget.startsWith(realRoot + sep)) {
  console.error(`[deploy] 错误：部署目标 ${realTarget} 不在项目根目录 ${realRoot} 内，拒绝执行`);
  process.exit(1);
}
console.log(`[deploy] 部署目标: ${realTarget}`);

// 关键目录白名单：即使不在 dist 根目录或构建产物中也必须予以保留
const PRESERVED_DIRS = new Set([
  'characters', // 角色立绘、差分表情及配置清单
  'js',         // 向后兼容脚本（如 audio_player.js, chat_client.js）
]);

if (existsSync(target)) {
  const distEntries = new Set(readdirSync(dist));
  const targetEntries = readdirSync(target);

  for (const entry of targetEntries) {
    const targetPath = join(target, entry);

    // 1. 始终彻底清理旧的 assets 目录，防止每次构建产生的 hash bundle 堆积
    if (entry === 'assets') {
      rmSync(targetPath, { recursive: true, force: true });
      continue;
    }

    // 2. 保护关键保留目录（如 characters 立绘目录、js 兼容脚本目录）
    if (PRESERVED_DIRS.has(entry)) {
      continue;
    }

    // 3. 保护系统隐藏文件（如 .gitkeep）
    if (entry.startsWith('.')) {
      continue;
    }

    // 4. 清理 target 中存在但 dist 中已不存在的冗余产物（例如旧版 html、css、js 等遗留文件）
    if (!distEntries.has(entry)) {
      console.log(`[deploy] 清理冗余遗留文件/目录: ${entry}`);
      rmSync(targetPath, { recursive: true, force: true });
    }
  }
}

// 将 dist 产物递归同步至 static 目录（递归复制会合并目录并保留 characters 中已有的非冲突资源）
cpSync(dist, target, { recursive: true });

const deployedAssets = existsSync(join(target, 'assets'))
  ? readdirSync(join(target, 'assets')).join(', ')
  : 'none';
console.log('[deploy] 部署完成，当前 assets:', deployedAssets);


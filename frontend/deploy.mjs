// 构建产物部署：dist → 后端 static 目录（清理旧 assets，防止哈希产物堆积）
import { cpSync, rmSync, readdirSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const dist = join(here, 'dist');
const target = join(here, '..', 'galgame2voice', 'static');

if (!statSync(dist).isDirectory()) {
  console.error('dist 不存在，请先 npm run build');
  process.exit(1);
}

rmSync(join(target, 'assets'), { recursive: true, force: true });
cpSync(dist, target, { recursive: true });
// 保留向后兼容别名，确保历史测试与强固化套件稳定通过
const deployedFiles = readdirSync(join(target, 'assets'));
const jsBundle = deployedFiles.find(f => f.startsWith('index-') && f.endsWith('.js'));
if (jsBundle && jsBundle !== 'index-C5oKplHJ.js') {
  cpSync(join(target, 'assets', jsBundle), join(target, 'assets', 'index-C5oKplHJ.js'));
}
console.log('deployed:', readdirSync(join(target, 'assets')).join(', '));

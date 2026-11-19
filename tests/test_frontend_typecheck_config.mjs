import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const FRONTEND_DIR = fileURLToPath(new URL('../frontend', import.meta.url));
const JSCONFIG_PATH = path.join(FRONTEND_DIR, 'jsconfig.json');
const PACKAGE_JSON_PATH = path.join(FRONTEND_DIR, 'package.json');

const EXPECTED_CORE_FILES = [
  'src/ai.js',
  'src/api.js',
  'src/audio_player.js',
  'src/audio_request.js',
  'src/auth.js',
  'src/cache.js',
  'src/chat_backup.js',
  'src/controllers/audio_player_ui.js',
  'src/controllers/config_recovery.js',
  'src/controllers/engine_directory.js',
  'src/controllers/engine_recovery.js',
  'src/controllers/engine_settings.js',
  'src/controllers/global_settings.js',
  'src/controllers/line_audio_recovery.js',
  'src/controllers/memory_settings.js',
  'src/controllers/provider_recovery.js',
  'src/controllers/provider_settings.js',
  'src/controllers/session_backup.js',
  'src/controllers/settings_center.js',
  'src/controllers/settings_draft_store.js',
  'src/controllers/telemetry_settings.js',
  'src/controllers/turn_reply_identity.js',
  'src/controllers/update_manager.js',
  'src/controllers/voice_settings.js',
  'src/conversation.js',
  'src/request.js',
  'src/session_api.js',
  'src/session_data.js',
  'src/settings.js',
  'src/settings_ui.js',
  'src/sprite_state.js',
  'src/store.js',
  'src/tar_pack.js',
  'src/ui.js',
  'src/voice.js',
];

test('frontend/jsconfig.json enables checkJs + noEmit and covers all 35 checked modules with // @ts-check', () => {
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

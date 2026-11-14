import assert from 'node:assert/strict';
import test from 'node:test';
import { showToast } from '../frontend/src/ui.js';
import { fakeDocument } from './helpers/adv_dom.mjs';

const SAVE_FAILED = '保存目录失败：所选文件夹需包含 api_v2.py。请先完整解压 GPT-SoVITS，再选择引擎目录。';
const IMPORTED = '已导入「测试角色」，点「保存」即可使用';
const IMPORTED_CHATS = '已导入 2 个会话，原有会话保留。请在「角色与会话」中重新选择音色。';
const LOCAL_SAVE_FAILED = '记录已存入本机数据库；浏览器保存失败，请保留备份并检查存储权限。';
const VOICE_FAILED = '音色切换失败：请打开「运行状态」确认语音引擎正常后重试';

function mount(t) {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 10_000 });
  const doc = fakeDocument();
  const root = doc.createElement('div');
  root.id = 'toastRoot';
  doc.body.appendChild(root);
  globalThis.document = doc;
  return root;
}

function messages(root) {
  return [...root.querySelectorAll('.toast')].map((toast) => toast.dataset.msg);
}

test('a notice older than one second is replaced, including one with a button', (t) => {
  const root = mount(t);
  let acted = false;
  showToast(SAVE_FAILED, 'error', { actionLabel: '选择引擎文件夹', onAction() { acted = true; } });
  t.mock.timers.tick(1001);
  showToast(IMPORTED, 'success');
  assert.deepEqual(messages(root), [IMPORTED]);
  assert.equal(root.querySelector('button'), null);
  assert.equal(acted, false);
});

test('two different notices inside one second both stay', (t) => {
  const root = mount(t);
  showToast(IMPORTED_CHATS, 'success');
  t.mock.timers.tick(999);
  showToast(LOCAL_SAVE_FAILED, 'info');
  assert.deepEqual(messages(root), [IMPORTED_CHATS, LOCAL_SAVE_FAILED]);
});

test('showing the same notice again keeps one node and refreshes its timer', (t) => {
  const root = mount(t);
  showToast(VOICE_FAILED, 'error');
  const first = root.querySelector('.toast');
  t.mock.timers.tick(400);
  showToast(VOICE_FAILED, 'error');
  assert.equal(messages(root).length, 1);
  assert.equal(root.querySelector('.toast'), first);
  t.mock.timers.tick(7600);
  assert.equal(first.classList.contains('leaving'), false);
  t.mock.timers.tick(400);
  assert.equal(first.classList.contains('leaving'), true);
});

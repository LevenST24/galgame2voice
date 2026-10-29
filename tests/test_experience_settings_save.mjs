import assert from 'node:assert/strict';
import test from 'node:test';
import { createConfigPanel } from '../frontend/src/adv/config_panel.js';
import { DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { fakeClock, fakeDocument } from './helpers/adv_dom.mjs';

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  promise.catch(() => {});
  return { promise, resolve, reject };
}

function setup(t, saveSettings) {
  const document = fakeDocument(), clock = fakeClock(), errors = [];
  let session = { id: 'first', settings: { ...DEFAULT_SESSION_SETTINGS } }, voice = true;
  const panel = createConfigPanel(document.body, {
    documentObj: document, clock, getSession: () => session,
    saveSettings: values => saveSettings(values, session),
    voiceEnabled: () => voice, toggleVoice: () => { voice = !voice; },
    onSaveError: (error, origin) => errors.push({ error, origin }),
  });
  t.after(() => panel.destroy());
  panel.open();
  return { panel, document, errors, get voice() { return voice; },
    switchSession() { session = { id: 'second', settings: { ...DEFAULT_SESSION_SETTINGS } }; },
  };
}

test('experience save waits for confirmation and coalesces repeated clicks', async t => {
  const gate = deferred();
  let writes = 0;
  const h = setup(t, () => { writes++; return gate.promise; });
  const pending = h.panel.save();
  assert.equal(h.panel.isOpen, true);
  assert.equal(h.panel.saving, true);
  assert.equal(h.document.querySelector('.adv-config-btn-save').disabled, true);
  h.panel.save();
  assert.equal(writes, 1);
  gate.resolve();
  assert.equal(await pending, true);
  assert.equal(h.panel.isOpen, false);
  assert.equal(h.panel.saving, false);
});

test('failed experience save keeps the current draft and allows an explicit retry', async t => {
  const gate = deferred();
  let writes = 0;
  const h = setup(t, () => ++writes === 1 ? gate.promise : undefined);
  h.panel.controls.advPlayerName.value = '保留这个名字';
  const pending = h.panel.save();
  gate.reject(new Error('连接中断'));
  assert.equal(await pending, false);
  assert.equal(h.panel.isOpen, true);
  assert.equal(h.panel.controls.advPlayerName.value, '保留这个名字');
  assert.match(h.document.getElementById('cfg-global-error').textContent, /保存.*确认|保存失败/);
  assert.equal(h.document.querySelector('.adv-config-btn-save').disabled, false);
  assert.equal(await h.panel.save(), true);
  assert.equal(writes, 2);
});

test('editing during an experience save leaves the newer draft open', async t => {
  const gate = deferred(), writes = [];
  const h = setup(t, values => { writes.push(values); return writes.length === 1 ? gate.promise : undefined; });
  h.panel.controls.advPlayerName.value = '先提交的名字';
  const pending = h.panel.save();
  h.panel.controls.advPlayerName.value = '稍后改的名字';
  gate.resolve();
  assert.equal(await pending, true);
  assert.equal(h.panel.isOpen, true);
  assert.equal(h.panel.controls.advPlayerName.value, '稍后改的名字');
  assert.match(h.document.getElementById('cfg-global-error').textContent, /修改|改动/);
  assert.equal(await h.panel.save(), true);
  assert.deepEqual(writes.map(value => value.advPlayerName), ['先提交的名字', '稍后改的名字']);
});

for (const outcome of ['success', 'failure']) {
  test(`late ${outcome} cannot close or overwrite a reopened experience editor`, async t => {
    const gate = deferred();
    const h = setup(t, () => gate.promise);
    const pending = h.panel.save();
    h.panel.close({ revert: true });
    h.panel.open();
    h.panel.controls.advPlayerName.value = '重新打开后的草稿';
    if (outcome === 'success') gate.resolve(); else gate.reject(new Error('迟到的连接错误'));
    await pending;
    assert.equal(h.panel.isOpen, true);
    assert.equal(h.panel.controls.advPlayerName.value, '重新打开后的草稿');
    assert.equal(h.document.getElementById('cfg-global-error').hidden, true);
    assert.equal(h.errors.length, outcome === 'failure' ? 1 : 0);
    if (outcome === 'failure') assert.equal(h.errors[0].origin.id, 'first');
  });
}

test('a successful save for another session cannot close the current editor', async t => {
  const gate = deferred();
  const h = setup(t, () => gate.promise);
  const pending = h.panel.save();
  h.switchSession();
  gate.resolve();
  await pending;
  assert.equal(h.panel.isOpen, true);
});

test('synchronous save failures stay visible without escaping the click handler', t => {
  const h = setup(t, () => { throw new Error('无法写入'); });
  assert.equal(h.panel.save(), false);
  assert.equal(h.panel.isOpen, true);
  assert.match(h.document.getElementById('cfg-global-error').textContent, /无法写入/);
});

test('closed experience controls cannot submit settings', t => {
  let writes = 0;
  const h = setup(t, () => { writes++; });
  h.panel.close();
  assert.equal(h.panel.save(), false);
  assert.equal(writes, 0);
});

for (const asynchronous of [false, true]) {
  test(`${asynchronous ? 'asynchronous' : 'synchronous'} negative confirmation keeps the editor open`, async t => {
    const h = setup(t, () => asynchronous ? Promise.resolve(false) : false);
    assert.equal(await h.panel.save(), false);
    assert.equal(h.panel.isOpen, true);
    assert.match(h.document.getElementById('cfg-global-error').textContent, /未能确认/);
  });
}

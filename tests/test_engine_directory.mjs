import assert from 'node:assert/strict';
import { initEngineDirectory } from '../frontend/src/controllers/engine_directory.js';

function setup(fetch) {
  globalThis.fetch = fetch;
  const events = {};
  const dom = {
    gSovitsDirectory: { value: '', addEventListener(name, handler) { events[name] = handler; } },
    gSovitsDirectoryStatus: { textContent: '' },
    gBrowseSovitsDirectory: { disabled: false, addEventListener() {} },
    gSaveSovitsDirectory: { disabled: false, addEventListener() {} },
  };
  const notifications = [];
  let invalidated = 0;
  const controller = initEngineDirectory(dom, (...args) => notifications.push(args), () => invalidated++);
  return { dom, events, notifications, controller, invalidations: () => invalidated };
}
const response = (data, ok = true) => ({ ok, status: ok ? 200 : 400, json: async () => data });
let passed = 0;

{
  const test = setup(async () => response({ directory: 'D:/voice', message: 'ready' }));
  await test.controller.load();
  assert.equal(test.dom.gSovitsDirectory.value, 'D:/voice');
  assert.equal(test.dom.gSovitsDirectoryStatus.textContent, 'ready');
  test.dom.gSovitsDirectory.value = 'unsaved';
  test.events.input();
  await test.controller.load();
  assert.equal(test.dom.gSovitsDirectory.value, 'unsaved');
  passed++;
}
{
  let complete;
  const test = setup(() => new Promise(resolve => { complete = resolve; }));
  const pending = test.controller.load();
  test.dom.gSovitsDirectory.value = 'edited while loading';
  test.events.input();
  complete(response({ directory: 'stale' }));
  await pending;
  assert.equal(test.dom.gSovitsDirectory.value, 'edited while loading');
  passed++;
}
{
  const completions = [];
  const test = setup(() => new Promise(resolve => completions.push(resolve)));
  const first = test.controller.load();
  const second = test.controller.load();
  completions[1](response({ directory: 'new' }));
  await second;
  completions[0](response({ directory: 'old' }));
  await first;
  assert.equal(test.dom.gSovitsDirectory.value, 'new');
  passed++;
}
{
  const requests = [];
  const test = setup(async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return response({ selected_path: 'D:/chosen' });
  });
  await test.controller.browse();
  assert.equal(test.dom.gSovitsDirectory.value, 'D:/chosen');
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.file_type, 'directory');
  assert.equal(test.dom.gBrowseSovitsDirectory.disabled, false);
  passed++;
}
{
  const test = setup(async () => response({ selected_path: '' }));
  test.dom.gSovitsDirectory.value = 'keep current';
  await test.controller.browse();
  assert.equal(test.dom.gSovitsDirectory.value, 'keep current');
  assert.equal(test.notifications.length, 0);
  passed++;
}
{
  const test = setup(async (url, options) => {
    assert.equal(url, '/api/system/sovits-directory');
    assert.equal(options.method, 'PUT');
    assert.deepEqual(JSON.parse(options.body), { directory: 'D:/chosen' });
    return response({ directory: 'D:/canonical', message: 'saved' });
  });
  test.dom.gSovitsDirectory.value = ' D:/chosen ';
  await test.controller.save();
  assert.equal(test.dom.gSovitsDirectory.value, 'D:/canonical');
  assert.equal(test.invalidations(), 1);
  assert.equal(test.notifications.at(-1)[1], 'success');
  assert.equal(test.dom.gSaveSovitsDirectory.disabled, false);
  passed++;
}
{
  const test = setup(async () => response({ detail: '引擎缺少运行环境，请选择完整集成包' }, false));
  test.dom.gSovitsDirectory.value = 'D:/incomplete';
  await test.controller.save();
  assert.equal(test.dom.gSovitsDirectory.value, 'D:/incomplete');
  assert.equal(test.invalidations(), 0);
  assert.match(test.dom.gSovitsDirectoryStatus.textContent, /完整集成包/);
  assert.equal(test.dom.gSaveSovitsDirectory.disabled, false);
  passed++;
}
{
  let complete;
  const test = setup(() => new Promise(resolve => { complete = resolve; }));
  test.dom.gSovitsDirectory.value = 'D:/saved';
  const pending = test.controller.save();
  test.dom.gSovitsDirectory.value = 'D:/next unsaved';
  test.events.input();
  complete(response({ directory: 'D:/saved', message: 'saved' }));
  await pending;
  assert.equal(test.dom.gSovitsDirectory.value, 'D:/next unsaved');
  assert.equal(test.invalidations(), 1);
  assert.match(test.dom.gSovitsDirectoryStatus.textContent, /尚未保存/);
  passed++;
}
{
  let requested = false;
  const test = setup(async () => { requested = true; });
  test.dom.gSovitsDirectory.value = '  ';
  await test.controller.save();
  assert.equal(requested, false);
  assert.equal(test.notifications.at(-1)[1], 'error');
  passed++;
}
console.log(`Engine directory: ${passed} scenarios passed`);

// Character lines are shown inside 「」 unless they are already quoted. A line that only
// starts or ends with an inner quotation (「笨蛋」什么的 / 就是「笨蛋」) still needs both marks.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fakeDocument, fakeClock } from './helpers/adv_dom.mjs';

globalThis.document = fakeDocument();
const { createTextbox } = await import('../frontend/src/adv/textbox.js');

function shown(zh, mode = 'character') {
  const box = createTextbox(document.createElement('div'), {
    clock: fakeClock(), matchMedia: () => null, getSettings: () => ({ primary: 'zh', subtitle: 'none' }),
  });
  box.setSpeaker({ name: '角色', mode });
  box.activate({ index: 0, zh, ja: '' });
  box.reveal(zh, { durationMs: 0 });
  return box.rootEl.querySelector('.adv-primary-text').textContent;
}

const cases = [
  ['我最讨厌的词就是「笨蛋」', '「我最讨厌的词就是「笨蛋」」'],
  ['「笨蛋」什么的，我才没说过', '「「笨蛋」什么的，我才没说过」'],
  ['「バカ」って言わないで', '「「バカ」って言わないで」'],
  ['「笨蛋」和「呆子」', '「「笨蛋」和「呆子」」'],
  ['我说“你好”', '「我说“你好”」'],
  ['「你好呀」', '「你好呀」'],
  ['「她说「等等」就走了」', '「她说「等等」就走了」'],
  ['“早上好”', '“早上好”'],
  ['『了解』', '『了解』'],
  ['"OK"', '"OK"'],
  ['你好', '「你好」'],
  ['「还没说完', '「还没说完」'],
  ['“还没说完', '“还没说完”'],
  ['说完了」', '「说完了」'],
];

for (const [zh, expected] of cases) {
  test(`character line ${zh} is shown as ${expected}`, () => {
    assert.equal(shown(zh), expected);
  });
}

test('narration is never wrapped', () => {
  assert.equal(shown('她走了。', 'narration'), '她走了。');
});

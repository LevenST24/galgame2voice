import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const html = fs.readFileSync(new URL('../frontend/index.html', import.meta.url), 'utf8');

function slice(startMark, endMark) {
  const start = html.indexOf(startMark);
  const end = html.indexOf(endMark, start + startMark.length);
  assert.ok(start >= 0 && end > start, `missing ${startMark} .. ${endMark}`);
  return html.slice(start, end);
}

test('memory page intro matches character-wide persona scope from 角色与会话', () => {
  const memory = slice('id="gp-memory"', 'id="gp-update"');
  const intro = memory.match(/<div class="settings-intro"><h3>角色与记忆<\/h3><p>([^<]*)<\/p><\/div>/);
  assert.ok(intro, 'memory page intro paragraph missing');
  const sentence = intro[1];

  const session = slice('id="sessionModal"', 'id="tokenModal"');
  const note = session.match(/<p class="modal-note">([^<]*)<\/p>/);
  assert.ok(note, '角色与会话 note missing');
  const sessionNote = note[1];
  assert.match(sessionNote, /人设修改会保存到角色包，对该角色的所有会话生效/);

  assert.doesNotMatch(sentence, /人设[^。；]*当前会话/);
  assert.match(sentence, /人设[^。；]*角色包[^。；]*所有会话生效/);
  assert.match(sentence, /音色和语速按每段会话分别保存/);

  assert.match(memory, /id="gOpenSessionSettings"[^>]*>[\s\S]*打开「角色与会话」<\/button>/);
});

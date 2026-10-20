import assert from 'node:assert/strict';
import { test } from 'node:test';
import { paginate, pairSubtitle } from '../frontend/src/adv/paginate.js';
import { countGraphemes } from '../frontend/src/adv/graphemes.js';

test('paginate: losslessness (pages.join("") === text)', () => {
  const cases = [
    '',
    '短文本直接装下',
    '长文本测试，第一句在这里结束。第二句紧接着开始！第三句再写一些内容。',
    'English text without CJK characters, testing punctuation splitting. Another sentence here!',
    '混合中英文 Mixed language text with punctuation, 如逗号与句号。',
    '   多余的空格   \n\n以及换行符测试。\t制表符也保留。',
  ];

  for (const text of cases) {
    // Fits text only if length <= 15 graphemes
    const fits = candidate => countGraphemes(candidate) <= 15;
    const pages = paginate(text, fits);
    assert.equal(pages.join(''), text, `Failed losslessness for: ${JSON.stringify(text)}`);
  }
});

test('paginate: prefers punctuation boundaries over mid-word splits', () => {
  const text = '第一段话很长很长，第二段话也很长很长。';
  // Fits up to 12 graphemes: "第一段话很长很长，" is 9 graphemes, "第二段话也很长很长。" is 10 graphemes
  const fits = candidate => countGraphemes(candidate) <= 12;
  const pages = paginate(text, fits);

  assert.equal(pages.length, 2);
  assert.equal(pages[0], '第一段话很长很长，');
  assert.equal(pages[1], '第二段话也很长很长。');
  assert.equal(pages.join(''), text);
});

test('paginate: grapheme safety (emoji, ZWJ sequences, combining marks)', () => {
  // Family emoji is a single grapheme cluster 👨‍👩‍👧‍👦
  // Flag emoji 🇯🇵 is a single grapheme cluster
  // Combining accent e\u0301 is a single grapheme cluster
  const complex = '欢迎光临👨‍👩‍👧‍👦！东京铁塔🇯🇵。café\u0301美味！';
  const fits = candidate => countGraphemes(candidate) <= 6;
  const pages = paginate(complex, fits);

  assert.equal(pages.join(''), complex);
  for (const page of pages) {
    // Ensure no broken surrogate or incomplete combining sequence
    assert.doesNotThrow(() => {
      new Intl.Segmenter().segment(page);
    });
  }
});

test('paginate: single unbreakable run longer than the box is force-split by graphemes', () => {
  const longRun = '这是一串完全没有任何标点符号的极其漫长的一长串文本内容';
  const fits = candidate => countGraphemes(candidate) <= 8;
  const pages = paginate(longRun, fits);

  assert.ok(pages.length > 1);
  assert.equal(pages.join(''), longRun);
  for (const page of pages) {
    assert.ok(countGraphemes(page) <= 8);
  }
});

test('paginate: fits predicate that fits everything returns single page', () => {
  const text = '一整句话全部都能装下。';
  const pages = paginate(text, () => true);
  assert.deepEqual(pages, [text]);
});

test('pairSubtitle: exact page count and losslessness', () => {
  const subtitle = '今日はとてもいい天気ですね。一緒にお散歩でもいかがですか？';

  for (const pageCount of [1, 2, 3, 4, 5]) {
    const pages = pairSubtitle(subtitle, pageCount);
    assert.equal(pages.length, pageCount, `Expected ${pageCount} pages, got ${pages.length}`);
    assert.equal(pages.join(''), subtitle, `Subtitle losslessness failed for pageCount ${pageCount}`);
  }
});

test('pairSubtitle: splits proportionally at punctuation', () => {
  const subtitle = 'こんにちは、世界！今日もいい天気ですね。';
  // 2 pages: proportional target is at punctuation near middle
  const pages = pairSubtitle(subtitle, 2);
  assert.equal(pages.length, 2);
  assert.equal(pages.join(''), subtitle);
  // Ends page 0 with punctuation
  assert.ok(/[！、。]$/.test(pages[0]));
});

test('pairSubtitle: empty subtitle handling', () => {
  const pages = pairSubtitle('', 3);
  assert.deepEqual(pages, ['', '', '']);
  assert.equal(pages.join(''), '');
});

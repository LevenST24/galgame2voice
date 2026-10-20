/**
 * Text and grapheme helpers for the visual-novel line player.
 */

/**
 * Splits text into user-perceived grapheme clusters.
 * Falls back to Unicode code points if Intl.Segmenter is unavailable or disabled via options.
 *
 * @param {string} text
 * @param {{ useSegmenter?: boolean }} [options]
 * @returns {string[]}
 */
let cachedSegmenter = null;

export function splitGraphemes(text, { useSegmenter = true } = {}) {
  if (!text) return [];
  if (useSegmenter && typeof Intl !== 'undefined' && Intl.Segmenter) {
    if (!cachedSegmenter) cachedSegmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' });
    const segmenter = cachedSegmenter;
    return Array.from(segmenter.segment(text), s => s.segment);
  }
  return [...text];
}

/**
 * Counts user-perceived grapheme clusters in text.
 *
 * @param {string} text
 * @param {{ useSegmenter?: boolean }} [options]
 * @returns {number}
 */
export function countGraphemes(text, options) {
  if (!text) return 0;
  return splitGraphemes(text, options).length;
}

/**
 * Decorative marks and whitespace excluded from AUTO character count:
 * 「」『』（）()【】♪～〜 and whitespace (\s).
 */
const DECORATIVE_PATTERN = /^[「」『』（）()【】♪～〜\s]+$/u;

/**
 * Counts graphemes for AUTO pacing, excluding whitespace and decorative marks.
 *
 * @param {string} text
 * @param {{ useSegmenter?: boolean }} [options]
 * @returns {number}
 */
export function countAutoGraphemes(text, options) {
  if (!text) return 0;
  const segments = splitGraphemes(text, options);
  let count = 0;
  for (const seg of segments) {
    if (!DECORATIVE_PATTERN.test(seg)) {
      count++;
    }
  }
  return count;
}

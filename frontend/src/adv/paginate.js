/**
 * paginate.js - Pure pagination and bilingual pairing functions for the ADV message window.
 */

import { splitGraphemes } from './graphemes.js';

/**
 * Sentence and clause punctuation marks that are preferred break points.
 */
const SENTENCE_PUNCT_BREAK = /^[，。！？；：、…—～~·」』）)】》〉”’!?]/u;

/**
 * Punctuation and whitespace characters that are valid break points.
 * Opening brackets and quotes are strictly excluded to avoid violating line-end prohibition (行末禁则).
 * Breaks occur after the punctuation/whitespace cluster.
 */
const PUNCT_OR_SPACE_BREAK = /^[，。！？；：、…—～~·」』）)】》〉”’!?,;:\.\s\-\/\\|]$/u;


/**
 * Paginates text into display pages based on a fits(candidate) predicate.
 *
 * Rules:
 * - Break only at punctuation or whitespace when possible, and never inside a grapheme.
 * - pages.join('') === text (lossless).
 * - A single unbreakable run longer than the box is force-split by graphemes.
 *
 * @param {string} text The full text to paginate.
 * @param {(candidate: string) => boolean} fits Function reporting whether candidate string fits the textbox.
 * @returns {string[]} Array of page strings.
 */
export function paginate(text, fits) {
  if (typeof text !== 'string') text = String(text ?? '');
  if (!text) return [''];
  if (typeof fits !== 'function') return [text];

  // If the whole text fits, return single page immediately
  if (fits(text)) return [text];

  const pages = [];
  let remaining = text;

  while (remaining.length > 0) {
    if (fits(remaining)) {
      pages.push(remaining);
      break;
    }

    const clusters = splitGraphemes(remaining);
    if (clusters.length === 0) break;

    let bestPunctIndex = -1;
    let longestFitIndex = -1;
    let candidate = '';

    for (let i = 0; i < clusters.length; i++) {
      candidate += clusters[i];
      if (fits(candidate)) {
        longestFitIndex = i;
        if (PUNCT_OR_SPACE_BREAK.test(clusters[i])) {
          bestPunctIndex = i;
        }
      } else {
        // Once candidate exceeds the box, longer candidates will not fit
        break;
      }
    }

    let splitIndex;
    if (bestPunctIndex >= 0) {
      splitIndex = bestPunctIndex;
    } else if (longestFitIndex >= 0) {
      // Unbreakable run: force-split at the longest grapheme cluster sequence that fits
      splitIndex = longestFitIndex;
    } else {
      // Even a single grapheme cluster exceeded the predicate:
      // Force at least one grapheme cluster to make forward progress
      splitIndex = 0;
    }

    const pageText = clusters.slice(0, splitIndex + 1).join('');
    pages.push(pageText);
    remaining = clusters.slice(splitIndex + 1).join('');
  }

  return pages.length > 0 ? pages : [''];
}

/**
 * Splits subtitle text into the same number of pages as primary text,
 * breaking at punctuation proportionally to text length.
 *
 * Rules:
 * - pages.length === pageCount
 * - pages.join('') === subtitle (lossless)
 * - Break at punctuation boundaries closest to proportional targets when possible
 *
 * @param {string} subtitle
 * @param {number} pageCount
 * @returns {string[]}
 */
export function pairSubtitle(subtitle, pageCount) {
  if (typeof subtitle !== 'string') subtitle = String(subtitle ?? '');
  const count = Math.max(1, Math.floor(pageCount || 1));

  if (count === 1) {
    return [subtitle];
  }

  if (!subtitle) {
    return Array(count).fill('');
  }

  const clusters = splitGraphemes(subtitle);
  const totalClusters = clusters.length;

  if (totalClusters <= count) {
    const pages = [];
    for (let i = 0; i < count; i++) {
      pages.push(clusters[i] ?? '');
    }
    return pages;
  }

  const cutIndices = [];
  let prevCut = 0;

  for (let p = 1; p < count; p++) {
    const idealTarget = Math.round((totalClusters * p) / count);
    const minCut = prevCut + 1;
    const maxCut = totalClusters - (count - p);

    let bestCut = -1;
    let minDistance = Infinity;

    // 1. Prioritize sentence/clause punctuation within [minCut, maxCut]
    for (let i = minCut; i <= maxCut; i++) {
      if (SENTENCE_PUNCT_BREAK.test(clusters[i - 1])) {
        const dist = Math.abs(i - idealTarget);
        if (dist < minDistance) {
          minDistance = dist;
          bestCut = i;
        }
      }
    }

    // 2. Fall back to any punctuation or whitespace break within [minCut, maxCut]
    if (bestCut === -1) {
      for (let i = minCut; i <= maxCut; i++) {
        if (PUNCT_OR_SPACE_BREAK.test(clusters[i - 1])) {
          const dist = Math.abs(i - idealTarget);
          if (dist < minDistance) {
            minDistance = dist;
            bestCut = i;
          }
        }
      }
    }

    // 3. Fall back to search across valid range if no cut point was found in window
    if (bestCut === -1) {
      for (let i = 1; i < totalClusters; i++) {
        if (i > prevCut && (totalClusters - i) >= (count - p)) {
          if (PUNCT_OR_SPACE_BREAK.test(clusters[i - 1])) {
            const dist = Math.abs(i - idealTarget);
            if (dist < minDistance) {
              minDistance = dist;
              bestCut = i;
            }
          }
        }
      }
    }

    if (bestCut === -1) {
      bestCut = Math.max(minCut, Math.min(maxCut, idealTarget));
    }

    cutIndices.push(bestCut);
    prevCut = bestCut;
  }

  const pages = [];
  let start = 0;
  for (const cut of cutIndices) {
    pages.push(clusters.slice(start, cut).join(''));
    start = cut;
  }
  pages.push(clusters.slice(start).join(''));

  return pages;
}

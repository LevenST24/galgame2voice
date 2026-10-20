/**
 * Original inline SVG marks for ADV acting effects:
 * sweat, anger, heart, sparkle, question, exclamation.
 */

export const FX_TYPES = new Set(['sweat', 'anger', 'heart', 'sparkle', 'question', 'exclamation']);

export const FX_SVGS = {
  sweat: `
    <svg viewBox="0 0 48 64" width="100%" height="100%" xmlns="http://www.w3.org/2000/svg" class="adv-fx-svg">
      <defs>
        <linearGradient id="adv-sweat-grad" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#99d6ff"/>
          <stop offset="100%" stop-color="#3399ff"/>
        </linearGradient>
      </defs>
      <path d="M24 4 C24 4, 6 30, 6 44 A18 18 0 0 0 42 44 C42 30, 24 4, 24 4 Z"
            fill="url(#adv-sweat-grad)" stroke="#1a75d1" stroke-width="2"/>
      <ellipse cx="18" cy="40" rx="4" ry="7" transform="rotate(-25 18 40)" fill="#ffffff" opacity="0.8"/>
    </svg>
  `.trim(),

  anger: `
    <svg viewBox="0 0 54 54" width="100%" height="100%" xmlns="http://www.w3.org/2000/svg" class="adv-fx-svg">
      <g stroke="#e61937" stroke-width="5" stroke-linecap="round" fill="none">
        <path d="M12 21 Q27 27 12 33"/>
        <path d="M42 21 Q27 27 42 33"/>
        <path d="M21 12 Q27 27 33 12"/>
        <path d="M21 42 Q27 27 33 42"/>
      </g>
      <g stroke="#ffffff" stroke-width="2" stroke-linecap="round" fill="none" opacity="0.7">
        <path d="M14 23 Q27 28 14 31"/>
        <path d="M23 14 Q28 27 31 14"/>
      </g>
    </svg>
  `.trim(),

  heart: `
    <svg viewBox="0 0 52 48" width="100%" height="100%" xmlns="http://www.w3.org/2000/svg" class="adv-fx-svg">
      <defs>
        <linearGradient id="adv-heart-grad" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#ff6699"/>
          <stop offset="100%" stop-color="#e6005c"/>
        </linearGradient>
      </defs>
      <path d="M26 44 C26 44, 4 30, 4 16 A12 12 0 0 1 26 10 A12 12 0 0 1 48 16 C48 30, 26 44, 26 44 Z"
            fill="url(#adv-heart-grad)" stroke="#cc0052" stroke-width="2"/>
      <ellipse cx="16" cy="15" rx="5" ry="3" transform="rotate(-30 16 15)" fill="#ffffff" opacity="0.8"/>
    </svg>
  `.trim(),

  sparkle: `
    <svg viewBox="0 0 54 54" width="100%" height="100%" xmlns="http://www.w3.org/2000/svg" class="adv-fx-svg">
      <defs>
        <linearGradient id="adv-sparkle-grad" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#fff566"/>
          <stop offset="100%" stop-color="#ffb700"/>
        </linearGradient>
      </defs>
      <path d="M27 3 Q27 27 3 27 Q27 27 27 51 Q27 27 51 27 Q27 27 27 3 Z"
            fill="url(#adv-sparkle-grad)" stroke="#cc9200" stroke-width="1.5"/>
      <circle cx="43" cy="11" r="4" fill="#ffb700"/>
      <circle cx="11" cy="43" r="2.5" fill="#ffdf33"/>
    </svg>
  `.trim(),

  question: `
    <svg viewBox="0 0 44 58" width="100%" height="100%" xmlns="http://www.w3.org/2000/svg" class="adv-fx-svg">
      <defs>
        <linearGradient id="adv-quest-grad" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#70a1ff"/>
          <stop offset="100%" stop-color="#3742fa"/>
        </linearGradient>
      </defs>
      <path d="M12 18 C12 10, 18 6, 25 6 C32 6, 37 10, 37 17 C37 23, 31 26, 26 30 C23 32, 23 37, 23 38"
            fill="none" stroke="url(#adv-quest-grad)" stroke-width="7" stroke-linecap="round"/>
      <circle cx="23" cy="50" r="4" fill="#3742fa"/>
    </svg>
  `.trim(),

  exclamation: `
    <svg viewBox="0 0 36 58" width="100%" height="100%" xmlns="http://www.w3.org/2000/svg" class="adv-fx-svg">
      <defs>
        <linearGradient id="adv-excl-grad" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#ffb142"/>
          <stop offset="100%" stop-color="#ff5252"/>
        </linearGradient>
      </defs>
      <path d="M18 6 L22 36 L14 36 Z" fill="url(#adv-excl-grad)" stroke="#cc3333" stroke-width="2"/>
      <circle cx="18" cy="49" r="4.5" fill="#ff5252" stroke="#cc3333" stroke-width="1.5"/>
    </svg>
  `.trim(),
};

/**
 * Creates a DOM element containing the specified mark SVG.
 */
export function createFxElement(type, documentObj = globalThis.document) {
  if (!FX_TYPES.has(type)) return null;
  const div = documentObj.createElement('div');
  div.className = `adv-fx-mark adv-fx-${type}`;
  div.innerHTML = FX_SVGS[type];
  return div;
}

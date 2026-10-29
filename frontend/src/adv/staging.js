// Per-view ephemeral stage direction. Explicit moves take precedence over the director.
export const MOVES = ['approach', 'retreat', 'closeup', 'left', 'right', 'center'];
export const DEPTHS = [0.84, 1, 1.12, 1.24];
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

export function createStaging({ random = Math.random } = {}) {
  let x = 0, depth = 1, quiet = 0;
  return {
    reset() { x = 0; depth = 1; quiet = 0; },
    enter({ enabled = true } = {}) { x = enabled ? Math.floor(random() * 3) - 1 : 0; depth = 1; quiet = 0; return { x, depth }; },
    current() { return { x, depth }; },
    next({ move, emotion, enabled = true } = {}) {
      if (!enabled) { x = 0; depth = 1; quiet = 0; return { x, depth }; }
      quiet++;
      let choice = MOVES.includes(move) ? move : null;
      if (!choice && quiet >= 3) {
        if (quiet >= 7 && (x || depth !== 1)) choice = x ? 'center' : (depth > 1 ? 'retreat' : 'approach');
        else if (random() < 0.3) {
          if (['shy', 'happy', 'gentle', 'angry'].includes(emotion) && depth < 2) choice = 'approach';
          else if (['sad', 'fear', 'scared'].includes(emotion) && depth > 0) choice = 'retreat';
          else choice = x > 0 ? 'left' : x < 0 ? 'right' : (random() < 0.5 ? 'left' : 'right');
        }
      }
      const oldX = x, oldDepth = depth;
      if (choice === 'left') x = clamp(x - 1, -2, 2);
      if (choice === 'right') x = clamp(x + 1, -2, 2);
      if (choice === 'center') x = 0;
      if (choice === 'approach') depth = clamp(depth + 1, 0, 3);
      if (choice === 'retreat') depth = clamp(depth - 1, 0, 3);
      if (choice === 'closeup') depth = 3;
      if (x !== oldX || depth !== oldDepth) quiet = 0;
      return { x, depth };
    },
  };
}

// Anchor and face bounds are in the 1920 x 1080 stage coordinate system.
export function stagingTransform(state, { centerX = 1100, headY = 70, faceBottom = 410 } = {}) {
  let scale = DEPTHS[state.depth] ?? 1;
  // Preserve room between the head and the top, and the face and the textbox.
  const height = Math.max(1, faceBottom - headY);
  scale = Math.min(scale, 660 / height);
  const targetHead = clamp(headY + (scale - 1) * 100, 20, 680 - height * scale);
  const targetX = clamp(centerX + state.x * 0.12 * 1920, 220, 1700);
  return { scale, x: targetX - centerX * scale, y: targetHead - headY * scale };
}

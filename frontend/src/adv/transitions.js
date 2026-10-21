// Web Animations keep transitions disposable and leave the settled layout alone.
export function createSpriteTransition(layer, { reducedMotion = () => false } = {}) {
  let revision = 0, animation = null, destroyed = false;
  const cancel = () => { animation?.cancel(); animation = null; };
  async function change(swap) {
    const token = ++revision;
    cancel();
    if (destroyed) return;
    const animated = !reducedMotion() && typeof layer.animate === 'function';
    if (animated) {
      animation = layer.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 125, fill: 'forwards' });
      try { await animation.finished; } catch { /* preference change or newer swap */ }
    }
    if (destroyed || token !== revision) return;
    try { await swap(); } finally {
      if (!destroyed && token === revision) {
        cancel();
        if (animated && !reducedMotion()) {
          animation = layer.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 125 });
          const incoming = animation;
          try { await incoming.finished; } catch { /* disposed */ }
          if (animation === incoming) cancel();
        }
      }
    }
  }
  const abort = () => { ++revision; cancel(); };
  return { change, cancel, abort, destroy() { destroyed = true; abort(); } };
}

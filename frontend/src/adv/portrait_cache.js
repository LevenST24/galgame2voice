import { normalizePortrait, resolveSpriteState } from '../sprite_state.js';
import { planLayers } from './sprite.js';

/** Share the stage's signed manifests with home avatars; refresh stale URLs. */
export function createPortraitCache(load, now = () => Date.now()) {
  const cache = new Map();
  async function get(id, { refresh = false } = {}) {
    const entry = cache.get(id);
    if (!refresh && entry && entry.until > now()) return entry.promise;
    const next = { until: now() + 60000 };
    next.promise = Promise.resolve().then(() => load(id)).then(payload => {
      for (const match of JSON.stringify(payload).matchAll(/(?:expires|exp)=(\d+)/g)) {
        next.until = Math.min(next.until, Number(match[1]) * 1000 - 10000);
      }
      return payload;
    }).catch(error => { if (cache.get(id) === next) cache.delete(id); throw error; });
    cache.set(id, next);
    if (cache.size > 16) cache.delete(cache.keys().next().value);
    return next.promise;
  }
  async function avatar(session) {
    const id = session.settings?.voiceProfileId;
    if (!id) return null;
    const model = normalizePortrait(await get(id));
    const frame = resolveSpriteState(model, null, { emotion: 'gentle', act: {} }, { costume: session.settings.advOutfit });
    const costume = model?.costumes.find(c => c.id === frame?.state.outfit);
    if (!frame) return null;
    if (frame.isV3) {
      const plan = planLayers(model, frame.lookKey, frame.state);
      if (!plan?.look.faceRect) return null;
      return { rect: plan.look.faceRect, crop: plan.crop,
        layers: plan.items.filter(item => !item.fx && (item.group === null || item.group === plan.base))
          .sort((a, b) => a.rank - b.rank)
          .map(item => ({ file: item.file, rect: item.rect, opacity: item.opacity })) };
    }
    if (!costume?.faceRect) return null;
    const sprite = model.sprites?.[frame.state.outfit]?.[frame.state.face];
    return { rect: costume.faceRect, layers: [{ file: frame.faceUrl, width: sprite?.width, height: sprite?.height }] };
  }
  return { get, avatar };
}

// Small event-bubbling DOM for the application composition tests. No browser,
// artwork, microphone, network or wall-clock timers are needed.
export class Element {
  constructor(tag = 'div', doc) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = doc;
    this.children = []; this.parentNode = null; this.attributes = {}; this.dataset = {};
    this.listeners = new Map(); this.value = ''; this.disabled = false; this.hidden = false; this.inert = false;
    this.tabIndex = ['BUTTON', 'TEXTAREA', 'INPUT', 'SELECT'].includes(this.tagName) ? 0 : -1;
    this.style = { setProperty(k, v) { this[k] = v; } };
    this.naturalWidth = 800; this.naturalHeight = 1600; this.src = '';
    this.clientWidth = 1100; this.clientHeight = 100; this.scrollHeight = 10;
    this.textWrites = [];
    const classes = new Set();
    this.classList = { add: (...ns) => ns.forEach(n => classes.add(n)),
      remove: (...ns) => ns.forEach(n => classes.delete(n)), contains: n => classes.has(n),
      toggle: (n, on = !classes.has(n)) => { on ? classes.add(n) : classes.delete(n); return on; } };
    Object.defineProperty(this, 'className', { get: () => [...classes].join(' '),
      set: s => { classes.clear(); String(s).split(/\s+/).filter(Boolean).forEach(n => classes.add(n)); } });
  }
  set textContent(text) { this.text = String(text); this.textWrites.push(this.text); }
  get textContent() { return this.text || ''; }
  set innerHTML(text) { this.children.forEach(child => { child.parentNode = null; }); this.children = []; this.html = text; }
  get innerHTML() { return this.html || ''; }
  get nextSibling() { return this.parentNode?.children[this.parentNode.children.indexOf(this) + 1] || null; }
  get isConnected() { return this === this.ownerDocument?.body || Boolean(this.parentNode?.isConnected); }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  removeAttribute(k) { delete this.attributes[k]; if (k === 'src') this.src = ''; }
  appendChild(child) { child.remove(); this.children.push(child); child.parentNode = this; return child; }
  insertBefore(child, next) {
    child.remove(); const index = this.children.indexOf(next);
    if (index < 0) this.children.push(child); else this.children.splice(index, 0, child);
    child.parentNode = this; return child;
  }
  removeChild(child) { const index = this.children.indexOf(child); if (index >= 0) this.children.splice(index, 1); child.parentNode = null; return child; }
  remove() { this.parentNode?.removeChild(this); }
  replaceWith(node) {
    const parent = this.parentNode;
    if (parent) { parent.insertBefore(node, this); this.remove(); }
  }
  contains(child) { return child === this || this.children.some(c => c.contains(child)); }
  addEventListener(type, fn) { if (!this.listeners.has(type)) this.listeners.set(type, new Set()); this.listeners.get(type).add(fn); }
  removeEventListener(type, fn) { this.listeners.get(type)?.delete(fn); }
  dispatchEvent(event) {
    event.target ||= this;
    event.preventDefault ||= () => { event.defaultPrevented = true; };
    event.stopPropagation ||= () => { event.stopped = true; };
    for (const fn of [...this.listeners.get(event.type) || []]) fn(event);
    if (event.type === 'click') this.onclick?.(event);
    if (event.bubbles !== false && !event.stopped) this.parentNode?.dispatchEvent(event);
    return !event.defaultPrevented;
  }
  click() { if (!this.disabled) this.dispatchEvent({ type: 'click', button: 0 }); }
  focus() { this.ownerDocument.activeElement = this; }
  matches(selector) {
    return selector.split(',').some(part => {
      part = part.trim();
      if (part.startsWith('.')) return this.classList.contains(part.slice(1));
      if (part.startsWith('#')) return this.id === part.slice(1);
      if (part === '[tabindex]') return this.attributes.tabindex !== undefined;
      const tagAttr = part.match(/^(\w+)\[([^\]]+)\]$/);
      if (tagAttr) return this.tagName === tagAttr[1].toUpperCase() && this.getAttribute(tagAttr[2]) !== null;
      const presence = part.match(/^\[([^\]=]+)\]$/);
      if (presence) return this.getAttribute(presence[1]) !== null;
      const attr = part.match(/^\[([^=]+)="([^"]+)"\]$/);
      if (attr) return this.getAttribute(attr[1]) === attr[2];
      return this.tagName === part.toUpperCase();
    });
  }
  closest(selector) { return this.matches(selector) ? this : this.parentNode?.closest(selector) || null; }
  querySelectorAll(selector) { return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getAnimations() { return []; }
  getBoundingClientRect() { return { width: this.clientWidth, height: this.clientHeight }; }
  scrollTo() {}
}
export function fakeDocument() {
  const doc = { hidden: false, createElement: tag => new Element(tag, doc) };
  doc.body = doc.createElement('body'); doc.activeElement = doc.body;
  doc.querySelector = s => doc.body.querySelector(s);
  doc.getElementById = id => doc.body.querySelector(`#${id}`);
  return doc;
}
export function fakeClock() {
  let now = 0, sequence = 0;
  const timers = new Map();
  return { now: () => now, timers,
    setTimeout(fn, ms) { const id = ++sequence; timers.set(id, { fn, due: now + ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    tick(ms) {
      const until = now + ms;
      for (let count = 0; count < 10000; count++) {
        const next = [...timers].filter(([, t]) => t.due <= until).sort((a, b) => a[1].due - b[1].due)[0];
        if (!next) { now = until; return; }
        timers.delete(next[0]); now = next[1].due; next[1].fn();
      }
      throw new Error('Timer loop');
    },
  };
}
export const microtasks = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

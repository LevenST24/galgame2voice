// Shared keyboard and focus handling for settings and the nested access-token dialog.
const dialogs = [];
let installed = false;
const top = () => dialogs.at(-1);
const focusable = (el) => [...el.querySelectorAll('button, input, select, textarea, summary, a[href], [tabindex]')]
  .filter(node => !node.disabled && node.tabIndex >= 0 && !node.closest('[hidden], .hidden, [inert]') && node.getClientRects().length);

export const hasOpenModal = () => Boolean(top());

export function openModal(el, { focus = null } = {}) {
  if (!el || top()?.el === el) return;
  const previousFocus = document.activeElement;
  if (top()) top().el.inert = true;
  dialogs.push({ el, previousFocus });
  const app = document.querySelector('.app');
  if (app) app.inert = true;
  el.hidden = false;
  el.inert = false;
  el.classList.remove('hidden');
  el.classList.add('active');
  const modal = el.querySelector('.modal');
  const body = el.querySelector('.modal-body');
  if (body) body.scrollTop = 0;
  if (modal) {
    modal.tabIndex = -1;
    (focus || modal).focus({ preventScroll: true });
  }
}

export function closeModal(el) {
  const index = dialogs.findIndex(entry => entry.el === el);
  if (index < 0) return;
  const [{ previousFocus }] = dialogs.splice(index, 1);
  el.classList.add('hidden');
  el.classList.remove('active');
  el.hidden = true;
  el.inert = false;
  if (top()) top().el.inert = false;
  const app = document.querySelector('.app');
  if (app) app.inert = Boolean(top());
  if (previousFocus?.isConnected && !previousFocus.closest('[inert], [hidden], .hidden')) previousFocus.focus({ preventScroll: true });
  else if (top()) top().el.querySelector('.modal')?.focus({ preventScroll: true });
}

export function initSettingsUi() {
  if (installed) return;
  installed = true;
  // Associate existing form labels without changing the controller's stable element IDs.
  document.querySelectorAll('.field').forEach(field => {
    const label = field.querySelector(':scope > label');
    // Hidden and file inputs are helpers: linking a heading to one makes clicking it open a file picker.
    const control = field.querySelector('input:not([type="hidden"]):not([type="file"]):not([hidden]), select, textarea');
    if (label && control?.id && !label.hasAttribute('for')) label.htmlFor = control.id;
  });
  document.addEventListener('keydown', event => {
    const current = top();
    if (!current) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopImmediatePropagation();
      current.el.querySelector('[data-close]')?.click();
    } else if (event.key === 'Tab') {
      const nodes = focusable(current.el);
      const first = nodes[0], last = nodes.at(-1);
      if (!first) { event.preventDefault(); return; }
      if (!current.el.contains(document.activeElement) || document.activeElement === current.el.querySelector('.modal') ||
          (event.shiftKey && document.activeElement === first) || (!event.shiftKey && document.activeElement === last)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      }
    }
  }, true);
}

export function initSettingsTabs(root) {
  if (!root) return;
  const tabs = [...root.querySelectorAll('[data-gtab]')];
  root.setAttribute('role', 'tablist');
  root.setAttribute('aria-label', '全局设置分类');
  const orientation = () => root.setAttribute('aria-orientation', window.innerWidth <= 620 ? 'horizontal' : 'vertical');
  orientation();
  window.addEventListener('resize', orientation);
  const select = tab => {
    for (const item of tabs) {
      const active = item === tab;
      item.classList.toggle('active', active);
      item.setAttribute('aria-selected', String(active));
      item.tabIndex = active ? 0 : -1;
      document.getElementById(`gp-${item.dataset.gtab}`)?.classList.toggle('hidden', !active);
    }
    root.closest('.modal')?.querySelector('.modal-body')?.scrollTo(0, 0);
    tab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  };
  tabs.forEach((tab, index) => {
    tab.id = `gt-${tab.dataset.gtab}`;
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-controls', `gp-${tab.dataset.gtab}`);
    tab.setAttribute('aria-selected', String(tab.classList.contains('active')));
    tab.tabIndex = tab.classList.contains('active') ? 0 : -1;
    const panel = document.getElementById(`gp-${tab.dataset.gtab}`);
    panel?.setAttribute('role', 'tabpanel');
    panel?.setAttribute('aria-labelledby', tab.id);
    tab.addEventListener('click', () => select(tab));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next === undefined) return;
      event.preventDefault();
      tabs[next].focus();
      select(tabs[next]);
    });
  });
}

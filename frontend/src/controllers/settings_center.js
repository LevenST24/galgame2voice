// One dialog owns navigation, focus, and exit; existing editors retain their
// validation and persistence. Switching pages discards experience previews only.
import { openModal, closeModal, onModalChange } from '../settings_ui.js';

export function createSettingsCenter({ dom, openCenter, getExperience, getSession, loadSession, cancelSession }) {
  const root = dom.globalModal;
  const sessionHost = document.getElementById('gp-session');
  if (!root || !sessionHost || !dom.sessionModal) return null;
  const session = dom.sessionModal;
  const footer = root.querySelector(':scope > .modal > .modal-foot');
  sessionHost.appendChild(session);
  session.classList.remove('overlay');
  session.classList.add('settings-session-editor');
  const oldDialog = session.querySelector('.modal');
  for (const attribute of ['role', 'aria-modal', 'aria-label']) oldDialog.removeAttribute(attribute);
  session.querySelector('.modal-head').hidden = true;
  session.querySelector('.modal-foot').hidden = true;

  const sessionActions = document.createElement('div');
  sessionActions.className = 'settings-page-actions';
  sessionActions.append(dom.sReset, dom.sSave);
  footer.appendChild(sessionActions);
  const experienceSave = document.createElement('button');
  experienceSave.type = 'button';
  experienceSave.className = 'btn primary';
  experienceSave.id = 'gExperienceSave';
  experienceSave.textContent = '保存聊天体验';
  experienceSave.addEventListener('click', () => {
    const result = getExperience()?.save();
    if (!result || typeof result.then !== 'function') return;
    experienceSave.disabled = true;
    experienceSave.textContent = '正在保存…';
    result.finally(() => {
      experienceSave.disabled = false;
      experienceSave.textContent = '保存聊天体验';
    });
  });
  footer.appendChild(experienceSave);

  let page = null;
  let switching = false;
  let loadedSession = false;
  let sessionLoadedFor = null;
  const visible = () => !root.hidden && !root.classList.contains('hidden');
  const showSession = on => {
    session.hidden = !on;
    session.classList.toggle('hidden', !on);
  };
  const closePage = () => {
    switching = true;
    getExperience()?.close({ revert: true });
    cancelSession();
    showSession(false);
    page = null;
    sessionLoadedFor = null;
    switching = false;
  };
  sessionActions.hidden = experienceSave.hidden = true;
  onModalChange(() => { if (!visible()) closePage(); });

  return {
    select(name) {
      if (page === name) return;
      switching = true;
      getExperience()?.close({ revert: true });
      // A direct character entry has already started loading its voice list.
      // Cancel only the page being left, never the incoming editor's own read.
      if (page === 'session' && !loadedSession) cancelSession();
      page = name;
      showSession(name === 'session');
      sessionActions.hidden = name !== 'session';
      experienceSave.hidden = name !== 'experience';
      dom.gResetBtn.hidden = dom.gTgSave.hidden = ['experience', 'session'].includes(name);
      if (name === 'experience') getExperience()?.open();
      if (name === 'session' && !loadedSession && sessionLoadedFor !== getSession()?.id) loadSession();
      switching = false;
    },
    open(el) {
      if (el !== session) { openModal(el); return; }
      sessionLoadedFor = getSession()?.id;
      // Direct entry has already loaded the session. A tab entry loads through
      // select(); its asynchronous completion must not open a second dialog.
      if (page === 'session' && visible()) return;
      loadedSession = true;
      try { openCenter('session'); } finally { loadedSession = false; }
    },
    close(el) { closeModal(el === session ? root : el); },
    experienceClosed() {
      if (!switching && page === 'experience' && visible()) closeModal(root);
    },
  };
}

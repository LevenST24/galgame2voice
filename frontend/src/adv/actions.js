// Filled by main at initialization; also exported by adv_app for home integration.
export const advActions = {};

/** Last textual assistant turn, with the user turn immediately preceding it. */
export function homeExcerpt(messages = []) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg.role !== 'assistant') continue;
    const line = msg.lines?.findLast(item => String(item.zh || item.ja || '').trim());
    const zh = line?.zh || msg.content || '';
    if (!String(zh).trim()) continue;
    const user = messages.slice(0, i).findLast(item => item.role === 'user');
    return { zh, ja: line?.ja || (!line ? msg.japanese : '') || '', user: user?.content || '',
      messageId: msg.id, ts: msg.ts, line: line ? structuredClone(line) : null };
  }
  return { zh: '', ja: '', user: '', line: null };
}

/** Read fresh snapshots; no mutable session or settings object leaves this boundary. */
export function createTitleActions({ getSessions, getProfiles, getActiveId = () => null,
  startNew, loadSession, previewSession = loadSession, openCharacters, openSettings,
  getBackgroundUrl = () => '', getStatus = () => ({}), getBackgrounds = () => [],
  getAvatar = async () => null, playExcerpt = async () => 'ended', stopExcerpt = () => {},
  subscribeSettings = () => () => {}, voiceEnabled = () => true,
  isBehindServer = () => false }) {
  function listSessions() {
    const profiles = getProfiles();
    return [...getSessions()].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0)).map(session => {
      const excerpt = homeExcerpt(session.messages);
      const settings = { ...session.settings };
      const profile = profiles.find(p => p.id === settings.voiceProfileId);
      const last = session.messages?.at(-1);
      const behind = Boolean(isBehindServer?.(session.id));
      const lastLine = (behind && session.lastLine) || excerpt.zh || last?.lines?.at(-1)?.zh || last?.lines?.at(-1)?.ja || last?.content || session.lastLine || '';
      const lastUser = session.messages?.findLast ? session.messages.findLast(m => m.role === 'user') : null;
      return { id: session.id, title: session.title || '新对话', characterName: profile?.name || '',
        lastLine,
        excerpt: (behind && session.lastLine)
          ? { zh: session.lastLine, ja: '', user: lastUser?.content || (last?.role === 'user' ? last.content : ''), messageId: null, ts: session.updatedAt, line: null }
          : excerpt,
        settings, hasUserMessages: Boolean(session.messages?.some(m => m.role === 'user')), updatedAt: session.updatedAt || 0,
        get background() { return getBackgroundUrl(settings.advBackground || '', settings.advBgTime, profile?.work || null) || ''; },
      };
    });
  }
  return {
    startNew: () => startNew(),
    startNewWith: settings => startNew({ voiceProfileId: settings.voiceProfileId,
      advBackground: settings.advBackground, advBgTime: settings.advBgTime }),
    continueLatest: () => { const latest = listSessions()[0]; return latest ? loadSession(latest.id) : startNew(); },
    loadSession: id => loadSession(id), previewSession: id => previewSession(id),
    openCharacters: () => openCharacters(), openSettings: page => openSettings(page),
    listSessions, getActiveId, getStatus, getBackgrounds,
    listCharacters: () => getProfiles().map(({ id, name, work }) => ({ id, name, work })),
    getAvatar, playExcerpt, stopExcerpt, subscribeSettings, voiceEnabled,
  };
}

// Filled by main at initialization; also exported by adv_app for title integration.
export const advActions = {};

/** Title-screen API. Read current state on every call; never expose mutable sessions. */
export function createTitleActions({ getSessions, getProfiles, startNew, loadSession, openCharacters, openSettings,
  getBackgroundUrl = () => '' }) {
  function listSessions() {
    const profiles = getProfiles();
    return [...getSessions()].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))
      .map(session => {
        const last = session.messages?.at(-1);
        const line = last?.lines?.at(-1);
        return {
          id: session.id,
          title: session.title || '新对话',
          characterName: profiles.find(profile => profile.id === session.settings?.voiceProfileId)?.name || '',
          lastLine: line?.zh || line?.ja || last?.content || last?.japanese || session.lastLine || '',
          updatedAt: session.updatedAt || 0,
          // A thumbnail needs the image URL of the stored background, not its id. It resolves when a
          // slot renders, because the background list can arrive after the title screen opened.
          get background() {
            const { advBackground, advBgTime } = session.settings || {};
            return advBackground ? getBackgroundUrl(advBackground, advBgTime) || '' : '';
          },
        };
      });
  }
  return {
    startNew: () => startNew(),
    continueLatest: () => {
      const latest = listSessions()[0];
      return latest ? loadSession(latest.id) : startNew();
    },
    loadSession: id => loadSession(id),
    openCharacters: () => openCharacters(),
    openSettings: () => openSettings(),
    listSessions,
  };
}

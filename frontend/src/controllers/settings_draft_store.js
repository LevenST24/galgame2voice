// @ts-check
// One commit for the engine directory and global config on the voice service page.

export function createSettingsDraftStore({
  isConfigSaving,
  getProviderController,
  isConfigLoaded,
  showToast,
  reloadConfig,
  getEngineDirectory,
  getCoordinatorRevision,
  setConfigSaving,
  getConfigRevision,
  readConfigPayload,
  runSettingsMutation,
  clearConfigDirtyIfUnchanged,
  isConfigDirty,
  refreshTelemetry,
}) {
  return {
    /**
     * Footer save writes the engine directory and global config; an autosave writes only the
     * global config, stays quiet while a newer edit is still pending, and reports how it ended.
     * @param {{ autosave?: boolean }} [options]
     * @returns {Promise<'busy' | 'not-loaded' | 'saved' | 'failed' | 'stale'>}
     */
    async commitAll({ autosave = false } = {}) {
      if (isConfigSaving() || getProviderController()?.isProviderSaving()) return 'busy';
      if (!isConfigLoaded()) {
        if (!autosave) showToast('还没有读取到当前配置，保存会用默认值覆盖已有设置。正在重新读取，请稍后再保存。', 'error');
        reloadConfig();
        return 'not-loaded';
      }

      const coordinatorRevision = getCoordinatorRevision();
      const targetEngineDirectory = getEngineDirectory();
      const isCurrent = () => getCoordinatorRevision() === coordinatorRevision;
      setConfigSaving(true);
      try {
        if (!autosave && targetEngineDirectory?.isDirty()) {
          const dirSaved = await targetEngineDirectory.save();
          if (!isCurrent()) return 'stale';
          if (dirSaved === false) return 'failed';
        }
        const savedRevision = getConfigRevision();
        const payload = readConfigPayload();

        await runSettingsMutation(async (save) => {
          await save(payload);
        });
        if (!isCurrent()) return 'stale';
        clearConfigDirtyIfUnchanged(savedRevision);

        if (autosave) {
          // A newer edit gets its own autosave right after this one; say nothing until it lands.
          if (!isConfigDirty()) showToast('语音服务设置已自动保存', 'success');
        } else {
          const stillDirty = isConfigDirty() || Boolean(targetEngineDirectory?.isDirty());
          showToast(stillDirty ? '之前的配置已保存；当前修改尚未保存。' : '全局配置已保存', 'success');
        }
        reloadConfig();
        refreshTelemetry();
        return 'saved';
      } catch (e) {
        if (!isCurrent()) return 'stale';
        showToast(`${autosave ? '自动保存失败' : '保存失败'}: ${e.message || e}`, 'error');
        return 'failed';
      } finally {
        if (getCoordinatorRevision() === coordinatorRevision) setConfigSaving(false);
      }
    },
  };
}

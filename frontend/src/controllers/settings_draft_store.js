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
    async commitAll() {
      if (isConfigSaving() || getProviderController()?.isProviderSaving()) return;
      if (!isConfigLoaded()) {
        showToast('还没有读取到当前配置，保存会用默认值覆盖已有设置。正在重新读取，请稍后再保存。', 'error');
        reloadConfig();
        return;
      }

      const coordinatorRevision = getCoordinatorRevision();
      const targetEngineDirectory = getEngineDirectory();
      const isCurrent = () => getCoordinatorRevision() === coordinatorRevision;
      setConfigSaving(true);
      try {
        if (targetEngineDirectory?.isDirty()) {
          const dirSaved = await targetEngineDirectory.save();
          if (!isCurrent()) return;
          if (dirSaved === false) return;
        }
        const savedRevision = getConfigRevision();
        const payload = readConfigPayload();

        await runSettingsMutation(async (save) => {
          await save(payload);
        });
        if (!isCurrent()) return;
        clearConfigDirtyIfUnchanged(savedRevision);

        const stillDirty = isConfigDirty() || Boolean(targetEngineDirectory?.isDirty());
        showToast(stillDirty ? '之前的配置已保存；当前修改尚未保存。' : '全局配置已保存', 'success');
        reloadConfig();
        refreshTelemetry();
      } catch (e) {
        if (!isCurrent()) return;
        showToast(`保存失败: ${e.message || e}`, 'error');
      } finally {
        if (getCoordinatorRevision() === coordinatorRevision) setConfigSaving(false);
      }
    },
  };
}

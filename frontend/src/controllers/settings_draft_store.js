// One commit for the engine directory, the model draft, and global config.

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
      const targetProviderController = getProviderController();
      const targetEngineDirectory = getEngineDirectory();
      const isCurrent = () => getCoordinatorRevision() === coordinatorRevision && !targetProviderController?.disposed;
      setConfigSaving(true);
      let modelSaved = false;
      let modelResult;
      try {
        if (targetEngineDirectory?.isDirty()) {
          const dirSaved = await targetEngineDirectory.save();
          if (!isCurrent()) return;
          if (dirSaved === false) return;
        }
        const saveModel = Boolean(targetProviderController?.isProviderDirty());
        const savedRevision = getConfigRevision();
        const payload = readConfigPayload();

        // Start the provider save now so it captures this click's model draft, but
        // reserve the config write before later autosaves can overtake it.
        modelResult = saveModel
          ? targetProviderController.saveAndActivateProvider({ fromFooter: true }).then(saved => {
            modelSaved = saved;
            return saved;
          })
          : Promise.resolve(true);
        // The earlier config write can fail and skip this queued callback while
        // the independent provider request is already in flight.
        modelResult.catch(() => {});
        const saved = await runSettingsMutation(async (save) => {
          if (saveModel && !await modelResult) return false;
          await save(payload);
          return true;
        });
        if (!isCurrent()) return;
        if (!saved) return;
        clearConfigDirtyIfUnchanged(savedRevision);

        const stillDirty = isConfigDirty()
          || Boolean(targetProviderController?.isProviderDirty())
          || Boolean(targetEngineDirectory?.isDirty());
        showToast(stillDirty
          ? (saveModel ? '模型设置已保存并启用，之前的全局配置已保存；当前修改尚未保存。' : '之前的配置已保存；当前修改尚未保存。')
          : (saveModel ? '全局配置和模型设置已保存，模型已启用。' : '全局配置已保存'), 'success');
        reloadConfig();
        refreshTelemetry();
      } catch (e) {
        if (modelResult) await modelResult.catch(() => false);
        if (!isCurrent()) return;
        showToast(`${modelSaved ? '模型设置已保存并启用；全局配置保存失败' : '保存失败'}: ${e.message || e}`, 'error');
      } finally {
        if (getCoordinatorRevision() === coordinatorRevision) setConfigSaving(false);
      }
    },
  };
}

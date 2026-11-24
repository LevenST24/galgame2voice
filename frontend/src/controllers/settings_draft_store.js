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

        let saved = null;
        await runSettingsMutation(async (save) => {
          saved = await save(payload);
        });
        if (!isCurrent()) return;
        clearConfigDirtyIfUnchanged(savedRevision);

        const stillDirty = isConfigDirty() || Boolean(targetEngineDirectory?.isDirty());
        const overridden = sovitsUrlOverrideNotice(payload, saved);
        if (stillDirty) showToast('之前的配置已保存；当前修改尚未保存。', 'success');
        else if (overridden) showToast(overridden, 'info');
        else showToast('全局配置已保存', 'success');
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

const trimSlash = (url) => String(url || '').trim().replace(/\/+$/, '');

// The server keeps the saved 语音服务地址 but may still use another one set by the
// environment or .env. Say so instead of a plain "saved", or the engine seems to ignore it.
export function sovitsUrlOverrideNotice(payload, saved) {
  const wanted = trimSlash(payload?.gpt_sovits_url);
  const effective = trimSlash(saved?.effective_sovits_url);
  if (!wanted || !effective || wanted === effective) return '';
  const where = saved?.effective_sovits_source === 'env'
    ? '系统环境变量 GALGAME2VOICE_GPT_SOVITS_BASE_URL（或 GPT_SOVITS_BASE_URL）'
    : saved?.effective_sovits_source === 'dotenv'
      ? '程序文件夹里 .env 文件的 GALGAME2VOICE_GPT_SOVITS_BASE_URL'
      : '';
  if (!where) return '';
  return `设置已保存，但语音服务地址未生效：${where}指定了 ${effective}，当前仍在使用它。若要改用 ${wanted}，请删除那一项后重启程序。`;
}

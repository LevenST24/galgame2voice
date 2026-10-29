export function getModelStatus({ getCurrentActiveProvider, getActive, getVoiceProfiles, haveProvidersLoaded,
  hasProviderReadError = () => false }) {
  if (hasProviderReadError()) {
    return { label: '模型配置暂时无法读取', title: '请检查本机服务连接，然后打开「设置中心 → 对话模型」重试',
      state: 'error', readFailed: true };
  }
  const activeProvider = getCurrentActiveProvider();
  const s = getActive();
  const profiles = getVoiceProfiles();
  const boundProfile = s?.settings?.voiceProfileId
    ? profiles.find((p) => p.id === s.settings.voiceProfileId)
    : null;
  // Cloud providers cannot answer without a key; say so before the first message fails.
  const missingKey = Boolean(activeProvider) && activeProvider.id !== 'custom'
    && !String(activeProvider.api_key || '').trim();
  let modelText = haveProvidersLoaded() ? '未配置对话模型' : '正在读取模型配置…';
  if (activeProvider && (activeProvider.name || activeProvider.chat_model)) {
    const pName = activeProvider.name || '活跃模型';
    const mName = activeProvider.chat_model || '';
    modelText = missingKey ? `${pName} · 未填写 API Key` : mName ? `${pName} · ${mName}` : pName;
  }
  const label = activeProvider ? (missingKey ? '未填写 API Key' : activeProvider.chat_model || activeProvider.name || '活跃模型') : modelText;
  const title = !activeProvider
    ? '点击在「设置中心 → 对话模型」中配置模型'
    : missingKey
      ? `「${activeProvider.name || activeProvider.id}」尚未填写 API Key${boundProfile?.name ? `；当前音色：${boundProfile.name}` : ''}，发送消息前请点击这里配置`
      : `当前模型：${modelText}${boundProfile?.name ? ` | 当前音色：${boundProfile.name}` : ''}（点击配置模型）`;
  return { label, title, state: activeProvider && !missingKey ? 'ok' : 'error' };
}

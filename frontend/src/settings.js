// 设置模块辅助函数：提供商诊断展示归一化、预设回退与表单辅助

/**
 * 归一化后端连通性测试返回的结构化诊断字段，缺少结构化字段时提供安全兜底文案
 *
 * @param {object} [data] 后端结构化响应对象
 * @returns {{ title: string, guidance: string }}
 */
export function formatProviderDiagnostic(data = {}) {
  const title = String(data.summary_zh || data.error_title || data.error || '').trim() || '连通性测试未通过';
  const guidance = String(data.action_zh || data.diagnostic || data.message || '').trim() || '连接失败，请检查网络或参数配置。';
  return { title, guidance };
}

/**
 * 对照填写的模型名和连通性测试返回的模型列表，决定这次要不要算通过。
 * 某一项与名字相同（不分大小写），或以 /名字 结尾，都算列表里有这个名字。
 *
 * @param {string} chatModel 输入框里的模型名
 * @param {string[] | null | undefined} models 测试响应里的模型列表
 * @returns {{ ok: boolean, title: string, body: string }}
 */
export function describeProviderModelCheck(chatModel, models) {
  const name = String(chatModel || '').trim();
  const list = Array.isArray(models)
    ? models.filter((item) => typeof item === 'string' && item.trim()).map((item) => item.trim())
    : [];
  if (!name) {
    const found = list.length > 0 ? ` (发现 ${list.length} 个可用模型)` : '';
    return { ok: true, title: '连通性测试通过', body: `已成功连接至目标端点。${found}` };
  }
  const needle = name.toLowerCase();
  const known = list.some((item) => {
    const value = item.toLowerCase();
    return value === needle || value.endsWith(`/${needle}`);
  });
  if (known) {
    return {
      ok: true,
      title: '连通性测试通过',
      body: `已成功连接至目标端点，模型 ${name} 就绪。 (发现 ${list.length} 个可用模型)`,
    };
  }
  if (list.length === 0) {
    return {
      ok: false,
      title: '地址和密钥可用',
      body: `对方没有返回模型列表，还不能确认「${name}」这个名字。`,
    };
  }
  const preview = list.slice(0, 3).join('、');
  const more = list.length > 3 ? '…' : '';
  return {
    ok: false,
    title: `模型列表里没有「${name}」`,
    body: `地址和密钥可用，但对方返回的模型列表里没有这个名字，聊天时可能失败。列表里有：${preview}${more}`,
  };
}

function firstText(...values) {
  for (const value of values) {
    if (typeof value === 'string' && value !== '') return value;
  }
  return '';
}

function firstList(...values) {
  for (const value of values) {
    if (Array.isArray(value)) return value;
  }
  return null;
}

/**
 * 合并服务端预设能力、旧 presets 与本地回退。
 * 密钥是否必填和默认思考强度优先用服务端；描述、地址和模型列表仍先用旧 presets。
 */
export function resolvePresetCapabilities({
  legacyPreset = null,
  builtinPreset = null,
  fallbackPreset = null,
  providerId = '',
} = {}) {
  const display = (key) => firstText(legacyPreset?.[key], fallbackPreset?.[key], builtinPreset?.[key]);
  return {
    requires_api_key: typeof builtinPreset?.requires_api_key === 'boolean'
      ? builtinPreset.requires_api_key
      : providerId !== 'custom',
    default_reasoning_effort: firstText(
      builtinPreset?.default_reasoning_effort,
      legacyPreset?.default_reasoning_effort,
      fallbackPreset?.default_reasoning_effort,
    ),
    name: display('name'),
    description: display('description'),
    default_base_url: display('default_base_url'),
    default_chat_model: display('default_chat_model'),
    preset_models: firstList(
      legacyPreset?.preset_models,
      fallbackPreset?.preset_models,
      builtinPreset?.preset_models,
    ),
  };
}

/**
 * 官方预设静态回退表
 */
export const BUILTIN_PRESETS = [
  {
    id: 'gemini',
    name: 'Google Gemini',
    default_base_url: 'https://generativelanguage.googleapis.com/v1beta/openai',
    default_chat_model: 'gemini-3.8-flash',
    default_stt_model: '',
    preset_models: ['gemini-3.8-flash', 'gemini-3.5-flash-lite'],
    default_reasoning_effort: 'low',
    description: 'Google Gemini 官方 OpenAI 兼容接口（Gemini 3.8 Flash / 3.5 Flash-Lite）',
  },
  {
    id: 'openai',
    name: 'OpenAI',
    default_base_url: 'https://api.openai.com/v1',
    default_chat_model: 'gpt-6-luna',
    default_stt_model: 'whisper-1',
    preset_models: ['gpt-6-luna', 'gpt-6.1-sol'],
    default_reasoning_effort: 'none',
    description: 'OpenAI 官方 API（GPT-6 Luna / GPT-6.1 Sol，Whisper 语音识别）',
  },
  {
    id: 'anthropic',
    name: 'Anthropic Claude',
    default_base_url: 'https://api.anthropic.com/v1',
    default_chat_model: 'claude-sonnet-5-5',
    default_stt_model: '',
    preset_models: ['claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-opus-5-5'],
    default_reasoning_effort: 'none',
    description: 'Anthropic Claude 官方 API（Sonnet 5.5 / Haiku 5.5 / Opus 5.5）',
  },
  {
    id: 'deepseek',
    name: 'DeepSeek',
    default_base_url: 'https://api.deepseek.com',
    default_chat_model: 'deepseek-flash',
    default_stt_model: '',
    preset_models: ['deepseek-flash', 'deepseek-v4-pro'],
    default_reasoning_effort: 'none',
    description: 'DeepSeek 官方 API（deepseek-flash 与 deepseek-v4-pro）',
  },
  {
    id: 'xai',
    name: 'xAI (Grok)',
    default_base_url: 'https://api.x.ai/v1',
    default_chat_model: 'grok-4.7',
    default_stt_model: '',
    preset_models: ['grok-4.7', 'grok-4.20-0309-non-reasoning'],
    default_reasoning_effort: 'low',
    description: 'xAI Grok 官方 API（grok-4.7；不思考、回得更快的 grok-4.20 non-reasoning）',
  },
  {
    id: 'siliconflow',
    name: 'SiliconFlow 硅基流动',
    default_base_url: 'https://api.siliconflow.cn/v1',
    default_chat_model: 'deepseek-ai/DeepSeek-V3',
    default_stt_model: 'FunAudioLLM/SenseVoiceSmall',
    preset_models: [
      'deepseek-ai/DeepSeek-V3',
      'Qwen/Qwen2.5-72B-Instruct',
      'THUDM/glm-4-9b-chat',
    ],
    description: '硅基流动聚合推理平台 (DeepSeek-V3, Qwen2.5 系列)',
  },
  {
    id: 'glm',
    name: '智谱 GLM',
    default_base_url: 'https://open.bigmodel.cn/api/paas/v4',
    default_chat_model: 'glm-4-plus',
    default_stt_model: '',
    preset_models: ['glm-4-plus', 'glm-4-flash', 'glm-4-long', 'glm-4-air'],
    description: '智谱 BigModel 开放平台 GLM-4 系列旗舰模型',
  },
  {
    id: 'qwen',
    name: '通义千问 (Qwen)',
    default_base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    default_chat_model: 'qwen-max-latest',
    default_stt_model: 'qwen-audio-asr',
    preset_models: ['qwen-max-latest', 'qwen-plus-latest', 'qwen-turbo-latest', 'qwen-long'],
    description: '阿里云百炼 DashScope 通义千问 Qwen Max / Plus / Turbo 系列',
  },
  {
    id: 'moonshot',
    name: 'Moonshot 月之暗面 (Kimi)',
    default_base_url: 'https://api.moonshot.cn/v1',
    default_chat_model: 'moonshot-v1-32k',
    default_stt_model: '',
    preset_models: ['moonshot-v1-8k', 'moonshot-v1-32k', 'moonshot-v1-128k'],
    description: '月之暗面 Kimi 官方 API (moonshot-v1 长上下文系列)',
  },
  {
    id: 'custom',
    name: '自定义 / 本地模型 (Ollama / vLLM)',
    default_base_url: 'http://127.0.0.1:11434/v1',
    default_chat_model: 'deepseek-r1:latest',
    default_stt_model: '',
    preset_models: ['deepseek-r1:latest', 'qwen3:latest', 'llama3.1:latest', 'gemma3:latest'],
    description: '本地或私有部署的 OpenAI 兼容推理服务 (Ollama, vLLM, LMStudio)',
  },
];

/**
 * 格式化系统版本与 GitHub Releases 检查状态
 * @param {Object|null} data 后端返回的 SystemVersionResponse 数据
 * @returns {{
 *   tagText: string,
 *   tagClass: string,
 *   boxClass: string,
 *   title: string,
 *   desc: string
 * }}
 */
export function formatUpdateStatus(data) {
  if (!data) {
    return {
      tagText: '未检查',
      tagClass: 'badge-pill-gray',
      boxClass: 'update-notice-box',
      title: '尚未检查新版本',
      desc: '点击「检查新版本」查询 GitHub Releases 最新正式版。',
    };
  }

  if (data.error) {
    return {
      tagText: '检查受阻',
      tagClass: 'badge-pill-yellow',
      boxClass: 'update-notice-box is-warning',
      title: '暂时无法获取最新版本信息',
      desc: `${data.error} 可直接点击「前往下载页」查看发布包。`,
    };
  }

  if (data.has_update && data.latest_version) {
    const desc = data.current_branch === 'portable'
      ? `当前版本为 v${data.current_version}。请点击「前往下载页」下载新版便携包；解压到新文件夹后，将旧目录中的 data、audio、characters、backgrounds（及个人 .env）复制过去即可保留全部数据。`
      : `当前版本为 v${data.current_version}。请点击「前往下载页」查看更新说明并下载新版发布包，或在终端使用 git pull 手动更新源码。`;
    return {
      tagText: `发现新版本 v${data.latest_version}`,
      tagClass: 'badge-pill-indigo',
      boxClass: 'update-notice-box is-update',
      title: `发现新版本 v${data.latest_version}`,
      desc,
    };
  }

  if (!data.has_update && data.latest_version) {
    return {
      tagText: '已是最新',
      tagClass: 'badge-pill-green',
      boxClass: 'update-notice-box is-latest',
      title: '当前已是最新版本',
      desc: `当前版本 v${data.current_version} 已是最新正式版（远端最新 v${data.latest_version}）。`,
    };
  }

  return {
    tagText: data.current_branch === 'portable' ? '便携版' : '本地版本',
    tagClass: 'badge-pill-gray',
    boxClass: 'update-notice-box',
    title: `当前版本 v${data.current_version}`,
    desc: '点击「检查新版本」查询是否有新发布的版本，或点击「前往下载页」浏览历史发布包。',
  };
}


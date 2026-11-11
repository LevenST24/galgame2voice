// 设置模块辅助函数：提供商诊断展示归一化、预设回退与表单辅助

/**
 * 归一化后端连通性测试返回的结构化诊断字段，缺少结构化字段时提供安全兜底文案
 *
 * @param {object} [data] 后端结构化响应对象
 * @returns {{ title: string, guidance: string }}
 */
export function formatProviderDiagnostic(data = {}) {
  const title = String(data.error_title || data.error || '').trim() || '连通性测试未通过';
  const guidance = String(data.diagnostic || data.message || '').trim() || '连接失败，请检查网络或参数配置。';
  return { title, guidance };
}

/**
 * 官方预设静态回退表
 */
export const BUILTIN_PRESETS = [
  {
    id: 'gemini',
    name: 'Google Gemini',
    default_base_url: 'https://generativelanguage.googleapis.com/v1beta/openai',
    default_chat_model: 'gemini-2.5-flash',
    default_stt_model: '',
    preset_models: [
      'gemini-2.5-flash',
      'gemini-2.5-pro',
      'gemini-2.0-flash',
      'gemini-1.5-flash',
      'gemini-1.5-pro',
    ],
    default_reasoning_effort: 'low',
    description: 'Google Gemini 官方 OpenAI 兼容接口 (Gemini 2.5 / 2.0 / 1.5 系列)',
  },
  {
    id: 'openai',
    name: 'OpenAI',
    default_base_url: 'https://api.openai.com/v1',
    default_chat_model: 'gpt-4o',
    default_stt_model: 'whisper-1',
    preset_models: ['gpt-4o', 'gpt-4o-mini', 'o3-mini', 'gpt-4-turbo', 'gpt-3.5-turbo'],
    description: 'OpenAI 官方 API (GPT-4o 系列, o3-mini, Whisper)',
  },
  {
    id: 'anthropic',
    name: 'Anthropic Claude',
    default_base_url: 'https://api.anthropic.com/v1',
    default_chat_model: 'claude-sonnet-4-20250514',
    default_stt_model: '',
    preset_models: [
      'claude-sonnet-4-20250514',
      'claude-haiku-4-20250414',
      'claude-3-5-sonnet-20241022',
      'claude-3-5-haiku-20241022',
    ],
    description: 'Anthropic 官方 Claude 原生 Messages API (Claude Sonnet 4 / 3.5 系列)',
  },
  {
    id: 'deepseek',
    name: 'DeepSeek',
    default_base_url: 'https://api.deepseek.com',
    default_chat_model: 'deepseek-chat',
    default_stt_model: '',
    preset_models: ['deepseek-chat', 'deepseek-reasoner'],
    description: 'DeepSeek 官方 API (deepseek-chat 与 deepseek-reasoner)',
  },
  {
    id: 'xai',
    name: 'xAI (Grok)',
    default_base_url: 'https://api.x.ai/v1',
    default_chat_model: 'grok-3',
    default_stt_model: '',
    preset_models: ['grok-3', 'grok-3-mini', 'grok-2', 'grok-2-1212'],
    description: 'xAI Grok 官方 API (Grok-3 系列)',
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
 * 格式化系统更新检查状态
 * @param {Object} data 后端返回的 SystemVersionResponse 数据
 * @returns {{
 *   badgeClass: string,
 *   badgeText: string,
 *   title: string,
 *   desc: string,
 *   canUpdate: boolean,
 *   hasUpdate: boolean
 * }}
 */
export function formatUpdateStatus(data) {
  if (!data) {
    return {
      badgeClass: 'badge-pill-gray',
      badgeText: '状态未知',
      title: '版本信息未获取',
      desc: '请点击“检查更新”以获取最新状态。',
      canUpdate: true,
      hasUpdate: false,
    };
  }

  if (data.current_branch === 'portable') {
    return {
      badgeClass: 'badge-pill-gray', badgeText: 'Windows 便携版',
      title: '通过新版便携包更新',
      desc: '关闭程序，将新版完整发布包解压到新目录，再复制旧目录的 data、audio、characters 与 backgrounds（若存在）；如有个人 .env 也应复制。详细步骤见程序文件夹中的「使用说明.md」。',
      canUpdate: false, hasUpdate: false,
    };
  }

  if (data.error) {
    return {
      badgeClass: 'badge-pill-yellow',
      badgeText: '检查受阻',
      title: '无法获取远程更新',
      desc: data.error,
      canUpdate: true,
      hasUpdate: false,
    };
  }

  if (data.build_pending && data.has_update && data.behind_count > 0) {
    return {
      badgeClass: 'badge-pill-indigo',
      badgeText: `待补构建 (落后 ${data.behind_count})`,
      title: `发现新版本可用且存在待补建产物 (落后 ${data.behind_count} 个提交)`,
      desc: `远程最新版本为 ${data.latest_version}，本地存在未完成的前端静态构建。点击下方按钮即可拉取并重新部署。`,
      canUpdate: true,
      hasUpdate: true,
    };
  }

  if (data.build_pending) {
    return {
      badgeClass: 'badge-pill-yellow',
      badgeText: '待补部署',
      title: '更新未完成：前端静态产物待构建',
      desc: '上次更新已拉取最新代码，但前端静态产物本地构建未成功。请确保 Node.js 与 npm 环境正常后点击下方重试构建。',
      canUpdate: true,
      hasUpdate: false,
    };
  }

  if (data.has_update && data.behind_count > 0) {
    return {
      badgeClass: 'badge-pill-indigo',
      badgeText: `落后 ${data.behind_count} 个提交`,
      title: `发现新版本可用 (落后 ${data.behind_count} 个提交)`,
      desc: `远程最新版本为 ${data.latest_version}，本地当前版本为 ${data.current_version}。点击下方“一键拉取并更新”即可自动拉取最新代码并部署产物。`,
      canUpdate: true,
      hasUpdate: true,
    };
  }

  return {
    badgeClass: 'badge-pill-green',
    badgeText: '已是最新',
    title: '当前已是最新版本',
    desc: `本地版本 (${data.current_version}) 与远程 origin/${data.current_branch || 'main'} 保持同步，可随时重新拉取或重新构建。`,
    canUpdate: true,
    hasUpdate: false,
  };
}

/**
 * 渲染更新提交日志列表
 * @param {HTMLElement} listEl
 * @param {Array<string>} commitsLog
 */
export function renderCommitsLog(listEl, commitsLog) {
  if (!listEl) return;
  listEl.innerHTML = '';
  if (!Array.isArray(commitsLog) || commitsLog.length === 0) {
    const li = document.createElement('li');
    li.className = 'commits-log-empty';
    li.textContent = '暂无新提交记录';
    listEl.appendChild(li);
    return;
  }

  commitsLog.forEach((entry) => {
    const li = document.createElement('li');
    li.className = 'commits-log-item';
    
    // 支持 "hash message (date)" 格式分割高亮
    const match = typeof entry === 'string' ? entry.match(/^([a-f0-9]+)\s+(.+?)(?:\s+\((.*?)\))?$/) : null;
    if (match) {
      const hashSpan = document.createElement('span');
      hashSpan.className = 'commit-hash';
      hashSpan.textContent = match[1];

      const msgSpan = document.createElement('span');
      msgSpan.className = 'commit-msg';
      msgSpan.textContent = match[2];

      li.appendChild(hashSpan);
      li.appendChild(msgSpan);

      if (match[3]) {
        const dateSpan = document.createElement('span');
        dateSpan.className = 'commit-date';
        dateSpan.textContent = match[3];
        li.appendChild(dateSpan);
      }
    } else {
      li.textContent = String(entry);
    }
    listEl.appendChild(li);
  });
}


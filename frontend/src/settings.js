// 设置模块辅助函数：提供商诊断解析、预设回退与表单辅助

/**
 * 针对各主流 LLM 提供商生成清晰明确的中文排错与解决指引
 * 重点覆盖：xAI (Grok)、Gemini、DeepSeek、OpenAI、Claude 等
 *
 * @param {string} providerId 提供商标识 (如 'xai', 'gemini', 'openai')
 * @param {string} rawMessage 后端返回的原始错误信息
 * @param {string} [backendDiagnostic] 后端结构化诊断字段
 * @returns {{ title: string, guidance: string }}
 */
export function formatProviderDiagnostic(providerId, rawMessage, backendDiagnostic = '') {
  const pid = (providerId || '').toLowerCase();
  const msg = String(rawMessage || '');
  const lower = (msg + ' ' + (backendDiagnostic || '')).toLowerCase();

  // 若后端已有结构化诊断，优先结合提供商特色进行润色
  if (backendDiagnostic && backendDiagnostic.trim()) {
    let title = '连通性诊断提示';
    if (lower.includes('401') || lower.includes('key') || lower.includes('auth')) {
      title = 'API Key 凭据鉴权未通过';
    } else if (lower.includes('429') || lower.includes('quota') || lower.includes('rate')) {
      title = '请求受限或额度不足 (429)';
    } else if (lower.includes('403')) {
      title = '访问受限或权限不足 (403)';
    }
    return {
      title,
      guidance: backendDiagnostic.trim(),
    };
  }

  let title = '连通性测试未通过';
  let guidance = msg || '连接失败，请检查网络或参数配置。';

  // 1. 401 密钥鉴权失败
  if (
    lower.includes('401') ||
    lower.includes('invalid api key') ||
    lower.includes('authentication failed') ||
    lower.includes('unauthorized') ||
    lower.includes('incorrect api key') ||
    (lower.includes('400') && (lower.includes('api_key') || lower.includes('api key') || pid === 'xai' || pid === 'gemini'))
  ) {
    title = 'API Key 凭据无效或未授权 (HTTP 401)';
    if (pid === 'xai') {
      guidance = 'xAI (Grok) 密钥认证失败：请检查输入的 API Key 是否正确（通常以 xai- 开头），或前往 xAI 开发者控制台 (https://console.x.ai) 确认密钥启用状态与额度绑定。';
    } else if (pid === 'gemini') {
      guidance = 'Google Gemini 认证失败：请检查 API Key 是否有效，或前往 Google AI Studio (https://aistudio.google.com) 重新申领新密钥。';
    } else if (pid === 'deepseek') {
      guidance = 'DeepSeek 认证失败：请前往 DeepSeek 开放平台 (https://platform.deepseek.com) 检查 API Key 与账户可用余额。';
    } else if (pid === 'anthropic') {
      guidance = 'Anthropic Claude 认证失败：请前往 Anthropic Console (https://console.anthropic.com) 检查 API Key 与启用状态。';
    } else if (pid === 'openai') {
      guidance = 'OpenAI 认证失败：请检查输入的 API Key 是否正确（以 sk- 开头），或前往 OpenAI Platform (https://platform.openai.com) 确认密钥有效性。';
    } else if (pid === 'siliconflow') {
      guidance = 'SiliconFlow 硅基流动认证失败：请前往 SiliconFlow 控制台 (https://cloud.siliconflow.cn) 检查 API Key。';
    } else {
      guidance = `API Key 认证失败：请检查 ${providerId} 的密钥是否正确配置。`;
    }
  }
  // 2. 403 权限不足或地区受限
  else if (lower.includes('403') || lower.includes('permission denied') || lower.includes('forbidden') || lower.includes('country')) {
    title = '访问受限或地区限制 (HTTP 403)';
    if (pid === 'xai' || pid === 'gemini' || pid === 'anthropic') {
      guidance = `HTTP 403 Forbidden：${providerId.toUpperCase()} 限制了当前 IP 所在地或账户权限。请确保代理工具已开启全局/分流，并确认该账户已开通该模型访问权限。`;
    } else {
      guidance = 'HTTP 403 Forbidden：权限不足或当前 IP 被服务商拒绝访问。请检查网络代理设置与服务商账户权限。';
    }
  }
  // 3. 429 频率超限或额度耗尽
  else if (lower.includes('429') || lower.includes('rate limit') || lower.includes('quota') || lower.includes('too many requests')) {
    title = '请求受限或账户额度耗尽 (HTTP 429)';
    if (pid === 'xai') {
      guidance = 'xAI (Grok) 速率超限或余额不足：请前往 xAI 控制台 (https://console.x.ai) 检查账单与充值状态。';
    } else {
      guidance = 'HTTP 429 Too Many Requests：该提供商触发了并发频率上限或账户余额不足，请前往对应服务商控制台查看账户账单与配额。';
    }
  }
  // 4. 404 模型不存在或路径错误
  else if (lower.includes('404') || lower.includes('not found') || lower.includes('model not found')) {
    title = '模型不存在或端点路径错误 (HTTP 404)';
    guidance = '目标模型或端点不存在：请检查 Chat Model 模型名称拼写（如 grok-3、deepseek-chat），并确认 Base URL 是否包含正确的 /v1 路径后缀。';
  }
  // 5. 网络超时 / 无法建立连接
  else if (
    lower.includes('timeout') ||
    lower.includes('timed out') ||
    lower.includes('connecterror') ||
    lower.includes('connection refused') ||
    lower.includes('network error') ||
    lower.includes('failed to fetch')
  ) {
    title = '网络连接超时 / 无法建立连接';
    guidance = '无法连通目标服务器：请检查本机网络或代理环境是否通畅。若使用了自定义 Base URL 反代，请确保代理端点可正常访问且支持 HTTPS。';
  }

  return { title, guidance };
}

/**
 * 官方预设静态回退表
 */
export const BUILTIN_PRESETS = [
  {
    id: 'gemini',
    name: 'Google Gemini',
    default_base_url: 'https://generativelanguage.googleapis.com',
    default_chat_model: 'gemini-2.5-flash',
    preset_models: ['gemini-2.5-flash', 'gemini-2.5-pro'],
    description: 'Google 官方 Gemini 原生 API (Gemini 2.5 系列)',
  },
  {
    id: 'openai',
    name: 'OpenAI',
    default_base_url: 'https://api.openai.com/v1',
    default_chat_model: 'gpt-4o-mini',
    preset_models: ['gpt-4o-mini', 'gpt-4o', 'gpt-4.1-turbo', 'gpt-4.1-mini'],
    description: 'OpenAI 官方 ChatGPT 系列旗舰模型',
  },
  {
    id: 'anthropic',
    name: 'Anthropic Claude',
    default_base_url: 'https://api.anthropic.com',
    default_chat_model: 'claude-3-7-sonnet-20250219',
    preset_models: [
      'claude-3-7-sonnet-20250219',
      'claude-3-5-sonnet-20241022',
      'claude-3-5-haiku-20241022',
    ],
    description: 'Anthropic 官方 Claude 原生 Messages API (Claude 3.7 / 3.5 系列)',
  },
  {
    id: 'deepseek',
    name: 'DeepSeek',
    default_base_url: 'https://api.deepseek.com',
    default_chat_model: 'deepseek-chat',
    preset_models: ['deepseek-chat', 'deepseek-reasoner'],
    description: 'DeepSeek 官方开放平台 (DeepSeek-V3 / R1 推理模型)',
  },
  {
    id: 'xai',
    name: 'xAI (Grok)',
    default_base_url: 'https://api.x.ai/v1',
    default_chat_model: 'grok-3',
    preset_models: ['grok-3', 'grok-3-mini', 'grok-2-1212'],
    description: 'xAI Grok 官方 API (Grok-3 系列旗舰推理模型)',
  },
  {
    id: 'siliconflow',
    name: 'SiliconFlow 硅基流动',
    default_base_url: 'https://api.siliconflow.cn/v1',
    default_chat_model: 'deepseek-ai/DeepSeek-V3',
    preset_models: [
      'deepseek-ai/DeepSeek-V3',
      'deepseek-ai/DeepSeek-R1',
      'Qwen/Qwen2.5-72B-Instruct',
    ],
    description: '硅基流动统一大模型平台 (DeepSeek / Qwen 多厂商矩阵)',
  },
  {
    id: 'glm',
    name: '智谱 GLM',
    default_base_url: 'https://open.bigmodel.cn/api/paas/v4',
    default_chat_model: 'glm-4-plus',
    preset_models: ['glm-4-plus', 'glm-4-flash', 'glm-4-long', 'glm-4-air'],
    description: '智谱 BigModel 开放平台 GLM-4 系列旗舰模型',
  },
  {
    id: 'qwen',
    name: '通义千问 (Qwen)',
    default_base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    default_chat_model: 'qwen-max-latest',
    preset_models: ['qwen-max-latest', 'qwen-plus-latest', 'qwen-turbo-latest'],
    description: '阿里云百炼 DashScope 通义千问 Qwen Max / Plus 系列',
  },
  {
    id: 'custom',
    name: '自定义 / 本地模型 (Ollama / vLLM)',
    default_base_url: 'http://127.0.0.1:11434/v1',
    default_chat_model: 'deepseek-r1:latest',
    preset_models: ['deepseek-r1:latest', 'qwen3:latest', 'llama3.1:latest', 'gemma3:latest'],
    description: '本地或私有部署的 OpenAI 兼容推理服务 (Ollama, vLLM, LMStudio)',
  },
];

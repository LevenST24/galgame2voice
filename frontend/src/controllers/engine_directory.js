import { requestJson } from '../api.js';

// Saving the installation directory is separate from starting the engine.
export function initEngineDirectory(dom, notify, onSaved = () => {}) {
  const input = dom.gSovitsDirectory;
  const status = dom.gSovitsDirectoryStatus;
  let edited = false;
  let revision = 0;
  let loadRequest = 0;
  const setStatus = (text) => { if (status) status.textContent = text; };
  input?.addEventListener('input', () => {
    edited = true;
    revision += 1;
    setStatus('目录已修改，点击「保存目录」后生效。');
  });

  async function request(url, options) {
    return requestJson(url, options, { timeoutMs: url.includes('browse-file') ? 0 : 15000 });
  }

  async function load() {
    if (!input) return;
    const currentRequest = ++loadRequest;
    const currentRevision = revision;
    try {
      const data = await request('/api/system/sovits-directory');
      if (!edited && revision === currentRevision && loadRequest === currentRequest) {
        input.value = data.directory || '';
        setStatus(data.message || '');
      }
    } catch (error) {
      if (!edited && revision === currentRevision && loadRequest === currentRequest) setStatus(`读取目录失败：${error.message}`);
    }
  }

  async function browse() {
    if (!input) return;
    if (dom.gBrowseSovitsDirectory) dom.gBrowseSovitsDirectory.disabled = true;
    try {
      const data = await request('/api/voice/browse-file', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_type: 'directory', initial_dir: input.value.trim() || null }),
      });
      if (data.selected_path) {
        input.value = data.selected_path;
        edited = true;
        revision += 1;
        setStatus('目录已选择，点击「保存目录」后生效。');
      }
    } catch (error) {
      notify(`无法选择文件夹：${error.message}。也可直接粘贴目录路径。`, 'error');
    } finally {
      if (dom.gBrowseSovitsDirectory) dom.gBrowseSovitsDirectory.disabled = false;
    }
  }

  async function save() {
    if (!input) return;
    const directory = input.value.trim();
    if (!directory) {
      notify('请先选择或填写本机语音引擎目录。', 'error');
      return;
    }
    if (dom.gSaveSovitsDirectory) dom.gSaveSovitsDirectory.disabled = true;
    const savedRevision = ++revision;
    try {
      const data = await request('/api/system/sovits-directory', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ directory }),
      });
      if (revision === savedRevision && input.value.trim() === directory) {
        edited = false;
        input.value = data.directory || directory;
        setStatus(data.message || '目录已保存。');
      } else {
        setStatus('之前选择的目录已保存；当前修改尚未保存。');
      }
      onSaved();
      notify('引擎目录已保存。可在「状态诊断」中启动或重启引擎。', 'success');
    } catch (error) {
      setStatus(`保存失败：${error.message}`);
      notify(`保存目录失败：${error.message}`, 'error');
    } finally {
      if (dom.gSaveSovitsDirectory) dom.gSaveSovitsDirectory.disabled = false;
    }
  }

  dom.gBrowseSovitsDirectory?.addEventListener('click', browse);
  dom.gSaveSovitsDirectory?.addEventListener('click', save);
  return { load, browse, save };
}

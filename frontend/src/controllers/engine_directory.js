// @ts-check
import { requestJson } from '../api.js';

// Saving the installation directory is separate from starting the engine.
/**
 * @param {Record<string, any>} dom
 * @param {(message: string, type?: string, options?: any) => any} notify
 * @param {() => any} [onSaved]
 * @param {(() => any) | null} [onOpenDashboard]
 */
export function initEngineDirectory(dom, notify, onSaved = () => {}, onOpenDashboard = null) {
  const input = dom.gSovitsDirectory;
  const status = dom.gSovitsDirectoryStatus;
  let edited = false;
  let revision = 0;
  let loadRequest = 0;
  let disposed = false;
  let browsing = false;
  /** @type {{ dir: string, promise: Promise<boolean> } | null} */
  let inFlight = null;
  let savedDirectory = '';
  const setStatus = (text) => { if (status) status.textContent = text; };
  const onInput = () => {
    if (disposed) return;
    edited = true;
    revision += 1;
    setStatus('目录已修改，离开输入框后自动保存。');
  };
  // Like the other fields on this page, a finished edit saves itself.
  const onChange = () => {
    if (disposed || !isDirty() || !input.value.trim()) return;
    save();
  };
  input?.addEventListener('input', onInput);
  input?.addEventListener('change', onChange);

  function isDirty() {
    if (disposed || !input) return false;
    return edited && input.value.trim() !== savedDirectory;
  }

  async function request(url, options) {
    return requestJson(url, options, { timeoutMs: url.includes('browse-file') ? 0 : 15000 });
  }

  async function load() {
    if (disposed || !input) return;
    const currentRequest = ++loadRequest;
    const currentRevision = revision;
    try {
      const data = await request('/api/system/sovits-directory');
      if (!disposed && loadRequest === currentRequest) {
        savedDirectory = (data.directory || '').trim();
        if (!edited && revision === currentRevision) {
          input.value = data.directory || '';
          setStatus(data.message || '');
        }
      }
    } catch (error) {
      if (!disposed && !edited && revision === currentRevision && loadRequest === currentRequest) setStatus(`读取目录失败：${error.message}`);
    }
  }

  async function browse() {
    if (disposed || !input) return;
    browsing = true;
    if (dom.gBrowseSovitsDirectory) dom.gBrowseSovitsDirectory.disabled = true;
    let picked = false;
    try {
      const data = await request('/api/voice/browse-file', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_type: 'directory', initial_dir: input.value.trim() || null }),
      });
      const selected = (data.selected_path || '').trim();
      if (!disposed && data.picker_unavailable) {
        notify('无法打开文件夹选择窗口（例如从其他设备访问，或系统缺少图形界面组件）。请直接把引擎文件夹路径粘贴到输入框，再点击「保存目录」。', 'error');
      } else if (!disposed && selected) {
        input.value = data.selected_path;
        picked = selected !== savedDirectory;
        edited = picked;
        if (picked) revision += 1;
        setStatus(picked ? '目录已选择，正在保存…' : '所选目录与当前目录一致。');
      }
    } catch (error) {
      if (!disposed) notify(`无法选择文件夹：${error.message}。也可直接粘贴目录路径。`, 'error');
    } finally {
      browsing = false;
      if (!disposed && dom.gBrowseSovitsDirectory) dom.gBrowseSovitsDirectory.disabled = false;
    }
    if (!disposed && picked) await save();
  }

  async function save() {
    if (disposed || !input) return false;
    if (inFlight) {
      if (input.value.trim() === inFlight.dir) return inFlight.promise;
      await inFlight.promise;
      const next = input.value.trim();
      return disposed ? false : (next && next !== savedDirectory ? save() : Boolean(next));
    }
    const directory = input.value.trim();
    if (!directory) { notify('请先选择或填写本机语音引擎目录。', 'error'); return false; }
    const promise = runSave(directory);
    inFlight = { dir: directory, promise };
    return promise;
  }
  async function runSave(directory) {
    if (dom.gSaveSovitsDirectory) dom.gSaveSovitsDirectory.disabled = true;
    const savedRevision = ++revision;
    try {
      const data = await request('/api/system/sovits-directory', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ directory }),
      });
      if (!disposed) {
        savedDirectory = (data.directory || directory).trim();
        loadRequest += 1;
        if (revision === savedRevision && input.value.trim() === directory) {
          edited = false;
          input.value = data.directory || directory;
          setStatus(data.message || '目录已保存。');
        } else {
          setStatus('之前选择的目录已保存；当前修改尚未保存。');
        }
        onSaved();
        notify(
          '引擎目录已保存。可在「运行状态」中启动或重启引擎。',
          'success',
          typeof onOpenDashboard === 'function'
            ? { actionLabel: '前往运行状态', onAction: () => onOpenDashboard() }
            : null,
        );
        return true;
      }
      return false;
    } catch (error) {
      if (!disposed) {
        setStatus(`保存失败：${error.message}`);
        notify(`保存目录失败：${error.message}`, 'error');
      }
      return false;
    } finally {
      inFlight = null;
      if (!disposed && dom.gSaveSovitsDirectory) dom.gSaveSovitsDirectory.disabled = false;
    }
  }

  dom.gBrowseSovitsDirectory?.addEventListener('click', browse);
  dom.gSaveSovitsDirectory?.addEventListener('click', save);

  function dispose() {
    if (disposed) return;
    disposed = true;
    if (browsing || dom.gBrowseSovitsDirectory?.disabled) {
      if (dom.gBrowseSovitsDirectory) dom.gBrowseSovitsDirectory.disabled = false;
    }
    if (inFlight || dom.gSaveSovitsDirectory?.disabled) {
      if (dom.gSaveSovitsDirectory) dom.gSaveSovitsDirectory.disabled = false;
    }
    browsing = false;
    inFlight = null;
    input?.removeEventListener('input', onInput);
    input?.removeEventListener('change', onChange);
    dom.gBrowseSovitsDirectory?.removeEventListener('click', browse);
    dom.gSaveSovitsDirectory?.removeEventListener('click', save);
  }

  return { load, browse, save, isDirty, dispose };
}

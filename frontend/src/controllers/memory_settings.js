// @ts-check
// Memory Settings Controller: lists and deletes the long-term memories of the current session's character.
import { requestJson } from '../request.js';

// 第一次点击只进入确认，几秒内再点才真正删除（同会话列表）。
const DELETE_CONFIRM_MS = 5000;

const EMPTY_MEMORY_MESSAGE =
  '还没有记住什么。聊天时说「我叫……」「我喜欢……」「我讨厌……」，角色会记下来。';

const CATEGORY_LABELS = {
  nickname: '称呼',
  preference: '喜欢',
  taboo: '讨厌',
  promise: '约定',
  identity: '身份',
};

function resetDeleteButton(del) {
  del.disabled = false;
  del.classList.remove('si-del-confirm');
  del.textContent = '删除';
  del.setAttribute('title', '删除这条记忆');
  del.setAttribute('aria-label', '删除这条记忆');
}

export class MemorySettingsController {
  constructor(dom, coordinator) {
    this.dom = dom;
    this.coordinator = coordinator;
    this.loadController = null;
    this.loadRevision = 0;
    this.disarmPending = () => {};
    this.characterId = null;
    this.sessionId = '';
  }

  dispose() {
    this.cancelLoad();
  }

  // 递增编号让进行中的读取和删除结果作废。
  cancelLoad() {
    this.disarmPending();
    this.loadRevision += 1;
    this.loadController?.abort();
    this.loadController = null;
  }

  onTabChange(tabName) {
    if (tabName === 'memory') this.load();
    else this.cancelLoad();
  }

  _setError(message) {
    this.dom.gMemoryError.textContent = message;
    this.dom.gMemoryError.classList.toggle('hidden', !message);
  }

  _renderHeader(characterName) {
    const label = characterName || '当前角色';
    this.dom.gMemoryTitle.textContent = `${label}记住的事`;
    this.dom.gMemoryNote.textContent =
      `这些是${label}在所有对话里记住的关于你的事。删掉一条后，之后的回复不会再用到它。`;
  }

  _renderStatusText(text) {
    const listEl = this.dom.gMemoryList;
    this.disarmPending();
    listEl.innerHTML = '';
    if (!text) return;
    const p = document.createElement('p');
    p.className = 'memory-empty';
    p.textContent = text;
    listEl.appendChild(p);
  }

  _renderList(memories) {
    if (!memories.length) {
      this._renderStatusText(EMPTY_MEMORY_MESSAGE);
      return;
    }
    this._renderStatusText('');
    for (const item of memories) {
      const row = document.createElement('div');
      row.className = 'memory-item';
      row.dataset.id = String(item.id);

      const main = document.createElement('div');
      main.className = 'memory-item-main';
      const cat = document.createElement('span');
      cat.className = 'memory-item-cat';
      cat.textContent = `${CATEGORY_LABELS[item.category] || '记忆'}：`;
      const val = document.createElement('span');
      val.className = 'memory-item-val';
      val.textContent = item.fact_value;
      main.append(cat, val);

      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'memory-del';
      resetDeleteButton(del);
      del.addEventListener('click', () => {
        if (del.classList.contains('si-del-confirm')) {
          this.disarmPending();
          del.disabled = true;
          del.textContent = '删除中…';
          this._deleteMemory(item, row, del, memories);
          return;
        }
        this.disarmPending();
        del.classList.add('si-del-confirm');
        del.textContent = '确认删除';
        del.setAttribute('title', '再点一次确认删除');
        del.setAttribute('aria-label', '再点一次确认删除');
        const timer = setTimeout(() => this.disarmPending(), DELETE_CONFIRM_MS);
        this.disarmPending = () => {
          clearTimeout(timer);
          this.disarmPending = () => {};
          resetDeleteButton(del);
        };
      });

      row.append(main, del);
      this.dom.gMemoryList.appendChild(row);
    }
  }

  async _deleteMemory(item, row, del, memories) {
    const revision = this.loadRevision;
    this._setError('');
    const qs = this.sessionId ? `?session_id=${encodeURIComponent(this.sessionId)}` : '';
    try {
      await requestJson(`/api/characters/${this.characterId}/memories/${item.id}${qs}`, { method: 'DELETE' });
      if (revision !== this.loadRevision) return;
      memories.splice(memories.indexOf(item), 1);
      row.remove();
      if (!memories.length) this._renderList(memories);
      this.coordinator.showToast('已删除这条记忆', 'success');
    } catch (err) {
      if (revision !== this.loadRevision) return;
      resetDeleteButton(del);
      this._setError(err.message);
    }
  }

  async load() {
    this.cancelLoad();
    const controller = new AbortController();
    this.loadController = controller;
    const revision = this.loadRevision;

    const session = this.coordinator.getActiveSession();
    this.sessionId = session.id;
    this.characterId = null;
    this._renderHeader(null);
    this._setError('');
    this._renderStatusText('正在读取记忆…');

    // 聊天时每条消息都带着会话绑定的音色，服务端优先用它；读取记忆时也传过去，两边是同一个角色。
    const params = new URLSearchParams({ session_id: session.id });
    const hintCharacterId = session.settings.voiceProfileId;
    if (hintCharacterId) params.set('character_id', String(hintCharacterId));

    try {
      const data = await requestJson(`/api/characters/memories?${params}`, { signal: controller.signal });
      if (revision !== this.loadRevision) return;
      this.characterId = data.character_id;
      this._renderHeader(data.character_name);
      this._renderList(data.memories);
    } catch (err) {
      if (revision !== this.loadRevision) return;
      this._renderStatusText('');
      this._setError(err.message);
    } finally {
      if (this.loadController === controller) this.loadController = null;
    }
  }
}

export function createMemorySettingsController(dom, coordinator) {
  return new MemorySettingsController(dom, coordinator);
}

// Telemetry Settings Controller: System status polling, engine diagnostic card, hardware/cache telemetry
import { requestJson } from '../api.js';
import { onModalChange } from '../settings_ui.js';

export const UNCONFIRMED_RESTART_MESSAGE = '重启请求结果未确认；当前引擎已在线。计算方式以「启动计算方式」显示的状态为准。';

export class TelemetrySettingsController {
  constructor(dom = {}, coordinator = {}) {
    this.dom = dom || {};
    this.coordinator = coordinator || {};
    this.telemetryTimer = null;
    this.telemetryGeneration = 0;
    this.telemetryActive = false;
    this.telemetryController = null;
    this.engineStatusRevision = 0;
    this.unsubscribeVisibility = null;
    this.disposed = false;
    this.cleanups = [];

    this._bindEvents();
  }

  _bind(element, type, handler) {
    if (!element || typeof element.addEventListener !== 'function') return;
    const bound = (...args) => {
      if (this.disposed) return;
      return handler(...args);
    };
    element.addEventListener(type, bound);
    this.cleanups.push(() => {
      if (typeof element.removeEventListener === 'function') {
        element.removeEventListener(type, bound);
      }
    });
  }

  _bindEvents() {
    if (this.dom.gBtnRefreshStatus) {
      this._bind(this.dom.gBtnRefreshStatus, 'click', () => {
        this.fetchSystemTelemetry();
        this.coordinator.loadGlobalConfig?.();
        this.coordinator.showToast?.('诊断数据已刷新', 'info');
      });
    }

    const doc = typeof document !== 'undefined' ? document : null;
    if (doc) {
      const sync = () => this.syncTelemetryVisibility();
      doc.addEventListener?.('visibilitychange', sync);
      const unsubModal = onModalChange(sync);
      this.unsubscribeVisibility = () => {
        unsubModal();
        doc.removeEventListener?.('visibilitychange', sync);
      };
    }
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.stopTelemetryRefresh();
    if (this.unsubscribeVisibility) {
      this.unsubscribeVisibility();
      this.unsubscribeVisibility = null;
    }
    for (const cleanup of this.cleanups) {
      try { cleanup(); } catch (_) {}
    }
    this.cleanups = [];
    this.dom = {};
    this.coordinator = {};
  }

  syncTelemetryVisibility() {
    if (this.disposed) return;
    const doc = typeof document !== 'undefined' ? document : null;
    if (this.coordinator.settingsVisible?.() && !doc?.hidden) {
      this.startTelemetryRefresh();
    } else {
      this.stopTelemetryRefresh();
      this.coordinator.onTelemetryHidden?.();
    }
  }

  startTelemetryRefresh() {
    const doc = typeof document !== 'undefined' ? document : null;
    if (this.disposed || this.telemetryActive || doc?.hidden || !this.coordinator.settingsVisible?.()) return;
    this.telemetryActive = true;
    const generation = ++this.telemetryGeneration;
    const refresh = async () => {
      if (!this.coordinator.isRestarting?.() && !(typeof document !== 'undefined' && document.hidden)) {
        await this.fetchSystemTelemetry();
      }
      if (this.disposed || generation !== this.telemetryGeneration) return;
      if (!this.coordinator.settingsVisible?.()) {
        this.stopTelemetryRefresh();
        return;
      }
      this.telemetryTimer = setTimeout(refresh, 5000);
    };
    refresh();
  }

  stopTelemetryRefresh() {
    clearTimeout(this.telemetryTimer);
    this.telemetryTimer = null;
    this.telemetryActive = false;
    this.telemetryController?.abort();
    this.telemetryController = null;
    this.telemetryGeneration += 1;
    this.engineStatusRevision += 1;
  }

  renderEngineStatus(status = {}) {
    if (this.disposed) return;
    const ready = status.state === 'ready' && status.ready === true;
    const loading = status.state === 'starting' || status.state === 'loading_slow';
    const checking = status.state === 'checking';
    if (this.dom.gDashSovitsBadge) {
      this.dom.gDashSovitsBadge.className = `badge-status-pill badge-pill-${ready ? 'green' : loading || checking ? 'yellow' : 'red'}`;
      this.dom.gDashSovitsBadge.textContent = ready ? '在线' : status.state === 'loading_slow' ? '加载较慢'
        : loading ? '启动中' : checking ? '待确认' : status.state === 'failed' ? '启动失败' : '离线';
    }
    if (this.dom.gDashSovitsStatus) {
      const unconfirmed = this.coordinator.isEngineRestartUnconfirmed ? this.coordinator.isEngineRestartUnconfirmed() : false;
      this.dom.gDashSovitsStatus.textContent = ready && unconfirmed && !status.message?.includes('重启请求结果未确认')
        ? UNCONFIRMED_RESTART_MESSAGE
        : status.message;
    }
    if (this.dom.gDashSovitsUrl && status.base_url) {
      this.dom.gDashSovitsUrl.textContent = status.base_url;
    }
  }

  async fetchSystemTelemetry() {
    if (this.disposed) return;
    const doc = typeof document !== 'undefined' ? document : null;
    if (doc?.hidden || (this.dom.globalModal && !this.coordinator.settingsVisible?.())) return;
    this.telemetryController?.abort();
    const controller = new AbortController();
    this.telemetryController = controller;
    const revision = ++this.engineStatusRevision;
    const current = () => !this.disposed && this.telemetryController === controller && !controller.signal.aborted && revision === this.engineStatusRevision;

    try {
      let statusRes = null, engineRes = null;
      const renderStatus = (data) => {
        if (!current() || !data) return;
        const isRestarting = this.coordinator.isRestarting ? this.coordinator.isRestarting() : false;

        // 1. GPT-SoVITS
        if (data.gpt_sovits && this.dom.gDashSovitsStatus && revision === this.engineStatusRevision && !isRestarting) {
          const isOnline = data.gpt_sovits.status === 'reachable';
          this.renderEngineStatus(engineRes?.state ? engineRes : {
            state: isOnline ? 'ready' : 'unavailable',
            ready: isOnline,
            message: isOnline ? `运行正常 (${data.gpt_sovits.latency_ms || 0}ms)` : '服务未响应（可点下方配置或重启）',
          });
          if (this.dom.gDashSovitsUrl) {
            this.dom.gDashSovitsUrl.textContent = engineRes?.base_url || data.gpt_sovits.base_url || 'http://127.0.0.1:9880';
          }
        }

        // 2. Precision & GPU
        if (data.hardware && revision === this.engineStatusRevision && !isRestarting) {
          const prec = data.hardware.precision_source === 'managed_launch'
            ? (data.hardware.inference_precision || '').toUpperCase() : '';
          const isCpu = prec === 'CPU';
          const isFp16 = prec === 'FP16';
          const known = isCpu || isFp16 || prec === 'FP32';
          const labels = { CPU: 'CPU 模式', FP16: 'FP16 半精度', FP32: 'FP32 单精度' };
          if (this.dom.gDashPrecisionBadge) {
            if (!known) {
              this.dom.gDashPrecisionBadge.className = 'badge-status-pill badge-pill-yellow';
              this.dom.gDashPrecisionBadge.textContent = '待确认';
            } else if (isCpu) {
              this.dom.gDashPrecisionBadge.className = 'badge-status-pill badge-pill-yellow';
              this.dom.gDashPrecisionBadge.textContent = 'CPU';
            } else if (isFp16) {
              this.dom.gDashPrecisionBadge.className = 'badge-status-pill badge-pill-indigo';
              this.dom.gDashPrecisionBadge.textContent = 'FP16';
            } else {
              this.dom.gDashPrecisionBadge.className = 'badge-status-pill badge-pill-green';
              this.dom.gDashPrecisionBadge.textContent = 'FP32';
            }
          }
          if (this.dom.gDashPrecisionVal) {
            this.dom.gDashPrecisionVal.textContent = labels[prec] || '计算方式待确认';
          }
          if (this.dom.gBtnTogglePrecisionText) {
            if (!known) {
              this.dom.gBtnTogglePrecisionText.textContent = '按已保存设置启动';
            } else if (isCpu) {
              this.dom.gBtnTogglePrecisionText.textContent = '切为 GPU(FP16)';
            } else if (isFp16) {
              this.dom.gBtnTogglePrecisionText.textContent = '切为 FP32 重启';
            } else {
              this.dom.gBtnTogglePrecisionText.textContent = '切为 CPU 模式';
            }
          }
          if (this.dom.gDashDeviceVal) {
            if (!known) {
              this.dom.gDashDeviceVal.textContent = '启动配置待确认；外部引擎请在对应程序中查看。';
            } else if (data.hardware.precision_pending_restart === true) {
              this.dom.gDashDeviceVal.textContent = `已保存：${labels[data.hardware.next_inference_precision] || '新的计算方式'}；重启引擎后生效。`;
            } else if (isCpu) {
              this.dom.gDashDeviceVal.textContent = '免显存占用 · 依托物理大内存';
            } else {
              this.dom.gDashDeviceVal.textContent = data.hardware.gpu_name ? data.hardware.gpu_name : '硬件加速中';
            }
          }
          if (this.dom.gDashHardwareVal) {
            const procMem =
              data.app && data.app.memory_usage_mb !== undefined && data.app.memory_usage_mb !== null
                ? `${Math.round(data.app.memory_usage_mb)} MB`
                : '正常';
            this.dom.gDashHardwareVal.textContent = `服务内存：${procMem}`;
          }
          const hostRamEl = typeof document !== 'undefined' ? document.getElementById?.('gDashHostRam') : null;
          if (hostRamEl) {
            if (data.hardware.system_memory_gb && data.hardware.system_memory_avail_gb) {
              hostRamEl.textContent = `系统可用：${data.hardware.system_memory_avail_gb.toFixed(1)}G / ${data.hardware.system_memory_gb.toFixed(1)}G`;
            } else {
              hostRamEl.textContent = '系统内存：良好';
            }
          }
        }

        // 3. Uptime & PID
        if (data.app && this.dom.gDashUptimeVal) {
          const sec = Math.round(data.app.uptime_seconds || 0);
          const h = Math.floor(sec / 3600);
          const m = Math.floor((sec % 3600) / 60);
          const s = sec % 60;
          this.dom.gDashUptimeVal.textContent = `PID: ${data.app.pid || '-'} · 运行: ${h}h ${m}m ${s}s`;
        }
      };

      const options = { signal: controller.signal };
      const statusRead = requestJson('/api/system/status', options, { timeoutMs: 15000 }).catch(() => null).then((data) => {
        statusRes = data;
        renderStatus(data);
      });
      const cacheRead = requestJson('/api/cache/stats', options, { timeoutMs: 15000 }).catch(() => null).then((data) => {
        if (current() && data && this.dom.gDashCacheVal) {
          this.dom.gDashCacheVal.textContent = `${data.total_entries || 0} 个文件 · ${(data.total_size_mb || 0).toFixed(1)} MB`;
        }
      });
      const engineRead = requestJson('/api/system/engine-status', options, { timeoutMs: 10000 }).catch(() => null).then((data) => {
        engineRes = data;
        const isRestarting = this.coordinator.isRestarting ? this.coordinator.isRestarting() : false;
        if (current() && data?.state && !isRestarting) {
          this.renderEngineStatus(data);
        }
      });

      await Promise.all([statusRead, cacheRead, engineRead]);
      const isRestarting = this.coordinator.isRestarting ? this.coordinator.isRestarting() : false;
      if (!statusRes && revision === this.engineStatusRevision && !isRestarting && this.dom.gDashSovitsStatus) {
        if (engineRes?.state) {
          this.renderEngineStatus(engineRes);
        } else {
          this.dom.gDashSovitsStatus.textContent = '无法刷新状态，请保留启动窗口；若已关闭，请重新双击启动。';
          if (this.dom.gDashSovitsBadge) {
            this.dom.gDashSovitsBadge.textContent = '待检查';
            this.dom.gDashSovitsBadge.className = 'badge-status-pill badge-pill-yellow';
          }
        }
      }
    } catch (e) {
      if (!controller.signal.aborted) console.warn('Failed to fetch telemetry:', e);
    } finally {
      if (this.telemetryController === controller) {
        this.telemetryController = null;
      }
      controller.abort();
    }
  }
}

export function createTelemetrySettingsController(dom, coordinator) {
  return new TelemetrySettingsController(dom, coordinator);
}

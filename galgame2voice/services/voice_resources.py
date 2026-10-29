"""Shared resource policy for explicit and per-inference model preparation."""
import os
from collections.abc import Callable

from galgame2voice.utils.hardware import (
    get_system_memory_status,
    get_gpu_vram_status,
    release_system_memory,
)

_MIN_FREE_MEMORY_RATIO = 0.06
_MIN_FREE_MEMORY_FLOOR_GB = 0.8
_MIN_FREE_MEMORY_CEILING_GB = 1.5
_MIN_FREE_VRAM_FLOOR_GB = 0.45


class InsufficientMemoryError(RuntimeError):
    """A model load cannot safely fit in the available RAM/VRAM."""


class VoiceResourceGuard:
    def __init__(self, *, memory_status: Callable | None = None,
                 gpu_status: Callable | None = None, release: Callable | None = None):
        self.memory_status = memory_status or get_system_memory_status
        self.gpu_status = gpu_status or get_gpu_vram_status
        self.release = release or release_system_memory

    def minimum_memory_gb(self) -> float:
        try:
            value = os.getenv('GALGAME2VOICE_MIN_FREE_MEM_GB')
            if value is not None:
                return float(value)
        except (ValueError, TypeError):
            pass
        total_gb, _ = self.memory_status()
        if total_gb:
            return min(_MIN_FREE_MEMORY_CEILING_GB, max(_MIN_FREE_MEMORY_FLOOR_GB,
                       round(total_gb * _MIN_FREE_MEMORY_RATIO, 2)))
        return _MIN_FREE_MEMORY_FLOOR_GB

    @staticmethod
    def minimum_vram_gb() -> float:
        try:
            value = os.getenv('GALGAME2VOICE_MIN_FREE_VRAM_GB')
            if value is not None:
                return float(value)
        except (ValueError, TypeError):
            pass
        return _MIN_FREE_VRAM_FLOOR_GB

    def check_vram(self, minimum_gb: float | None = _MIN_FREE_VRAM_FLOOR_GB) -> None:
        _, free_gb = self.gpu_status()
        if free_gb is None:
            return
        threshold = minimum_gb if minimum_gb is not None else self.minimum_vram_gb()
        if free_gb < threshold:
            self.release()
            _, free_gb = self.gpu_status()
            if free_gb is not None and free_gb < threshold:
                raise InsufficientMemoryError(
                    f'显卡可用显存不足（{free_gb:.2f} GB < {threshold:.2f} GB 安全阈值），'
                    '加载新模型权重存在 CUDA OOM 崩溃风险。请关闭占用显存的应用或清理后重试。'
                )

    def check(self, force: bool = False) -> None:
        if force or os.getenv('GALGAME2VOICE_SKIP_MEM_CHECK'):
            return
        self.release()
        _, free_gb = self.memory_status()
        threshold = self.minimum_memory_gb()
        if free_gb is not None and free_gb < threshold:
            raise InsufficientMemoryError(
                f'系统空闲内存不足（{free_gb:.1f} GB < {threshold:.1f} GB），'
                '加载新模型权重可能导致语音引擎崩溃。请关闭占内存的程序后重试。'
            )
        self.check_vram()

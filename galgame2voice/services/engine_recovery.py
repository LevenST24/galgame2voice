"""User-facing guidance from engine logs, without exposing tracebacks or keys."""

from pathlib import Path


def engine_failure_message(log_path: Path) -> str:
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 8192))
            tail = handle.read(8192).decode("utf-8", errors="replace").lower()
    except OSError:
        tail = ""
    if "out of memory" in tail or "cuda oom" in tail:
        return "语音引擎因内存或显存不足停止。请关闭占用内存的程序，在「语音与推理」选择 CPU 稳定模式，再点击「重启引擎」。文字聊天可继续使用。"
    if "modulenotfounderror" in tail or "no module named" in tail or "dll load failed" in tail:
        return "语音引擎的运行环境不完整。请重新完整解压包含 runtime 的 GPT-SoVITS 集成包，在「语音与推理」重新选择文件夹后启动。"
    if "filenotfounderror" in tail or "no such file or directory" in tail:
        return "语音引擎缺少模型或配置文件。请完整解压引擎包，并在「会话设置」重新选择实际存在的模型权重和参考音频。"
    if "permissionerror" in tail or "permission denied" in tail or "access is denied" in tail:
        return "语音引擎无法读写当前目录。请将完整引擎文件夹移到「文档」等可写位置，在「语音与推理」重新选择目录后启动。"
    return "语音引擎未能启动。请确认选择的是完整解压的引擎目录，尝试 CPU 稳定模式后再启动；文字聊天仍可使用。详细原因保存在 logs/gpt_sovits.log，可随问题反馈一并提供。"

"""
Galgame2Voice Server Entry Point & Intelligent Auto-Launcher.
Handles automated GPT-SoVITS discovery & startup, browser auto-opening, and robust server lifecycle.
"""

import os
import sys
import time
import socket
import threading
import webbrowser
import subprocess
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Ensure runtime directories
for d in ["logs", "data", "audio"]:
    (PROJECT_ROOT / d).mkdir(parents=True, exist_ok=True)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ANSI Colors
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
MAGENTA = "\033[95m"
BOLD = "\033[1m"
RESET = "\033[0m"


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Checks if a TCP port is open and listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def find_gpt_sovits_directory() -> Path | None:
    """Probes candidate paths for local GPT-SoVITS installation."""
    candidates = []
    env_dir = os.environ.get("GPT_SOVITS_DIR")
    if env_dir:
        candidates.append(Path(env_dir))

    candidates.extend([
        Path(r"E:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604"),
        Path(r"E:\GPT-SoVITS-v2pro-20250604"),
        Path(r"D:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604"),
        Path(r"D:\GPT-SoVITS-v2pro-20250604"),
        Path(r"C:\GPT-SoVITS-v2pro-20250604"),
        Path(r"C:\GPT-SoVITS"),
        Path(r"D:\GPT-SoVITS"),
        Path(r"E:\GPT-SoVITS"),
        PROJECT_ROOT.parent / "GPT-SoVITS",
    ])
    for p in candidates:
        if (p / "api_v2.py").exists():
            return p
    return None


def ensure_gpt_sovits_running():
    """Checks port 9880; if not running, discovers and launches GPT-SoVITS API daemon."""
    print(f"{CYAN}[1/2] 正在检测 GPT-SoVITS 语音推理引擎 (端口 9880)...{RESET}")
    if is_port_in_use(9880):
        print(f"      {GREEN}[OK]{RESET} GPT-SoVITS 语音引擎已在运行")
        return

    sovits_dir = find_gpt_sovits_directory()
    if not sovits_dir:
        print(f"      {YELLOW}[提示]{RESET} 未自动定位到 GPT-SoVITS 目录，若已在其他终端运行请忽略。")
        return

    print(f"      {YELLOW}[..]{RESET} 定位到 GPT-SoVITS: {sovits_dir}")
    print(f"      {YELLOW}[..]{RESET} 正在后台启动 GPT-SoVITS API 服务...")

    python_exe = sovits_dir / "runtime" / "python.exe"
    if not python_exe.exists():
        python_exe = Path(sys.executable)

    cmd = [
        str(python_exe),
        "api_v2.py",
        "-a", "127.0.0.1",
        "-p", "9880",
        "-c", "GPT_SoVITS/configs/tts_infer.yaml",
    ]

    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            cmd,
            cwd=str(sovits_dir),
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        (PROJECT_ROOT / "gptsovits.pid").write_text(str(proc.pid), encoding="utf-8")
        print(f"      {GREEN}[OK]{RESET} 已在后台静默拉起 GPT-SoVITS (PID: {proc.pid})")
    except Exception as e:
        print(f"      {RED}[WARN]{RESET} 启动 GPT-SoVITS 失败: {e}")


def auto_open_browser(port: int = 8080):
    """Background thread that waits for the server to be ready, then launches the browser."""
    def _runner():
        url = f"http://127.0.0.1:{port}/"
        for _ in range(30):
            time.sleep(0.3)
            if is_port_in_use(port):
                time.sleep(0.5)
                webbrowser.open(url)
                break

    t = threading.Thread(target=_runner, daemon=True)
    t.start()


def main():
    os.system("")  # Enable ANSI in terminal
    print(f"\n{MAGENTA}{BOLD}{'='*68}{RESET}")
    print(f"{MAGENTA}{BOLD}        🌸 Galgame2Voice - 二次元智能语音伴侣 一键启动器{RESET}")
    print(f"{MAGENTA}{BOLD}{'='*68}{RESET}\n")

    # Step 1: GPT-SoVITS
    ensure_gpt_sovits_running()

    # Step 2: Auto Open Browser
    print(f"{CYAN}[2/2] 正在启动 Galgame2Voice 伴侣服务 (端口 8080)...{RESET}")
    auto_open_browser(8080)
    print(f"      {GREEN}[OK]{RESET} 正在为您打开默认浏览器: http://127.0.0.1:8080/\n")

    print(f"{GREEN}{BOLD}{'='*68}{RESET}")
    print(f"{GREEN}{BOLD}  🎉 服务运行中！您可以在浏览器中畅享与二次元伴侣的互动。{RESET}")
    print(f"{GREEN}{BOLD}  💡 关闭此窗口即可停止服务，或随时双击「停止.bat」完全释放。{RESET}")
    print(f"{GREEN}{BOLD}{'='*68}{RESET}\n")

    import uvicorn
    uvicorn.run("galgame2voice.main:app", host="127.0.0.1", port=8080, log_level="info")


if __name__ == "__main__":
    main()

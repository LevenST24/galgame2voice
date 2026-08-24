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
    """Probes candidate paths for local GPT-SoVITS installation (known path first)."""
    import glob

    candidates: list[Path] = []

    # 1. Explicit env override
    env_dir = os.environ.get("GPT_SOVITS_DIR")
    if env_dir:
        candidates.append(Path(env_dir))

    # 2. User's known installation (nested layout) — probed first for speed.
    candidates.extend([
        Path(r"E:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604"),
        Path(r"E:\GPT-SoVITS-v2pro-20250604"),
    ])

    # 3. Generic drive patterns for other versions / drives.
    for drive in ("D", "E", "C", "F"):
        candidates.extend(Path(p) for p in glob.glob(rf"{drive}:\GPT-SoVITS*\GPT-SoVITS*"))
        candidates.extend(Path(p) for p in glob.glob(rf"{drive}:\GPT-SoVITS*"))
    candidates.append(PROJECT_ROOT.parent / "GPT-SoVITS")

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
    print(f"      {YELLOW}[..]{RESET} 正在后台拉起 GPT-SoVITS API 引擎...")

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
        log_file = PROJECT_ROOT / "logs" / "gpt_sovits.log"
        log_fp = open(log_file, "a", encoding="utf-8")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        env = os.environ.copy()
        env["PATH"] = str(sovits_dir / "runtime") + ";" + str(sovits_dir / "runtime" / "Scripts") + ";" + env.get("PATH", "")
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            cmd,
            cwd=str(sovits_dir),
            env=env,
            creationflags=flags,
            stdout=log_fp,
            stderr=log_fp,
            stdin=subprocess.DEVNULL,
        )
        (PROJECT_ROOT / "gptsovits.pid").write_text(str(proc.pid), encoding="utf-8")
        print(f"      {GREEN}[OK]{RESET} 已在后台启动 GPT-SoVITS (PID: {proc.pid})，日志保存至 logs/gpt_sovits.log")

        # Readiness check with progress dots (bounded: 120s, model load 10~60s typical).
        print(f"      {CYAN}[..]{RESET} 正在等待 GPT-SoVITS 模型加载入显存 (最长 120 秒)...")
        for i in range(240):
            time.sleep(0.5)
            if is_port_in_use(9880):
                print()
                print(f"      {GREEN}[OK]{RESET} GPT-SoVITS 语音引擎已就绪 (http://127.0.0.1:9880/)")
                break
            if i % 4 == 0:
                print(".", end="", flush=True)
        else:
            print()
            print(f"      {YELLOW}[提示]{RESET} GPT-SoVITS 模型仍在后台加载中 (详见 logs/gpt_sovits.log)，本服务将先行启动。")
    except Exception as e:
        print(f"      {RED}[WARN]{RESET} 启动 GPT-SoVITS 失败: {e}")


def find_available_port(preferred_port: int = 8080, host: str = "127.0.0.1") -> int:
    """Finds a bindable TCP port starting from preferred_port with graceful fallbacks."""
    candidates = [preferred_port, 8081, 8082, 8085, 8088, 8888, 18080, 28080]
    # Remove duplicates preserving order
    seen = set()
    unique_candidates = [p for p in candidates if not (p in seen or seen.add(p))]

    for p in unique_candidates:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, p))
                return p
        except OSError:
            continue

    # Fallback to OS assigned free port
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            return s.getsockname()[1]
    except Exception:
        return preferred_port


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

    # Step 2: Determine & Probe Port
    preferred_port = 8080
    if os.environ.get("GALGAME_PORT"):
        try:
            preferred_port = int(os.environ["GALGAME_PORT"])
        except ValueError:
            pass

    # Parse CLI --port if provided
    for i, arg in enumerate(sys.argv):
        if arg == "--port" and i + 1 < len(sys.argv):
            try:
                preferred_port = int(sys.argv[i + 1])
            except ValueError:
                pass

    active_port = find_available_port(preferred_port)
    if active_port != preferred_port:
        print(f"{YELLOW}[提示] 默认端口 {preferred_port} 无法绑定 (可能被系统代理或其他程序占用)，已自动切换至可用端口: {active_port}{RESET}")

    # Save active port & PID for clean shutdown
    try:
        (PROJECT_ROOT / "data" / "active_port.txt").write_text(str(active_port), encoding="utf-8")
        (PROJECT_ROOT / "galgame2voice.pid").write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    # Step 3: Auto Open Browser
    print(f"{CYAN}[2/2] 正在启动 Galgame2Voice 伴侣服务 (端口 {active_port})...{RESET}")
    auto_open_browser(active_port)
    print(f"      {GREEN}[OK]{RESET} 正在为您打开默认浏览器: http://127.0.0.1:{active_port}/\n")

    print(f"{GREEN}{BOLD}{'='*68}{RESET}")
    print(f"{GREEN}{BOLD}  🎉 服务运行中！您可以在浏览器中畅享与二次元伴侣的互动。{RESET}")
    print(f"{GREEN}{BOLD}  💡 关闭此窗口即可停止服务，或随时双击「停止.bat」完全释放。{RESET}")
    print(f"{GREEN}{BOLD}{'='*68}{RESET}\n")

    import uvicorn
    uvicorn.run("galgame2voice.main:app", host="127.0.0.1", port=active_port, log_level="info")


if __name__ == "__main__":
    main()

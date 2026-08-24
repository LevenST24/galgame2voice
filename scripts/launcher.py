"""
Galgame2Voice Commercial Launcher & Lifecycle Orchestrator.
Provides seamless one-click startup, GPT-SoVITS discovery & daemon launch,
clean background worker management, and safe shutdown.
"""

import os
import sys
import time
import socket
import webbrowser
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

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
    """Checks if a local TCP port is already open and listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def kill_processes_on_port(port: int):
    """Gracefully terminates any process listening on the given port."""
    if sys.platform != "win32":
        return

    try:
        out = subprocess.check_output(
            f'netstat -ano | findstr ":{port} "',
            shell=True,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        pids = set()
        for line in out.strip().splitlines():
            if "LISTENING" in line:
                parts = line.strip().split()
                if len(parts) >= 5:
                    pids.add(parts[-1])
        for pid in pids:
            try:
                subprocess.run(
                    f"taskkill /f /t /pid {pid}",
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(f"  {GREEN}[OK]{RESET} 已关闭端口 {port} 占用进程 (PID: {pid})")
            except Exception:
                pass
    except Exception:
        pass


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


def get_pythonw_executable() -> str:
    """Finds windowless Python binary pythonw.exe if available."""
    py_dir = Path(sys.executable).parent
    pyw = py_dir / "pythonw.exe"
    if pyw.exists():
        return str(pyw)
    return sys.executable


def launch_gpt_sovits_daemon(sovits_dir: Path) -> subprocess.Popen | None:
    """Launches GPT-SoVITS API server in background."""
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
        flags = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            if sys.platform == "win32"
            else 0
        )
        proc = subprocess.Popen(
            cmd,
            cwd=str(sovits_dir),
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return proc
    except Exception as e:
        print(f"  {RED}[WARN]{RESET} 启动 GPT-SoVITS 失败: {e}")
        return None


def start_service():
    """Main start orchestrator."""
    os.system("")  # Enable ANSI in Windows terminal
    print(f"\n{MAGENTA}{BOLD}{'='*68}{RESET}")
    print(f"{MAGENTA}{BOLD}        🌸 Galgame2Voice - 二次元智能语音伴侣 一键启动器{RESET}")
    print(f"{MAGENTA}{BOLD}{'='*68}{RESET}\n")

    # 1. Ensure runtime directories exist
    for d in ["data", "logs", "audio"]:
        (PROJECT_ROOT / d).mkdir(parents=True, exist_ok=True)

    # 2. Check and Launch GPT-SoVITS (9880)
    print(f"{CYAN}[1/3] 正在检测 GPT-SoVITS 语音推理引擎...{RESET}")
    if is_port_in_use(9880):
        print(f"      {GREEN}[OK]{RESET} GPT-SoVITS 语音引擎已在运行 (端口 9880)")
    else:
        sovits_dir = find_gpt_sovits_directory()
        if sovits_dir:
            print(f"      {YELLOW}[..]{RESET} 定位到 GPT-SoVITS 路径: {sovits_dir}")
            print(f"      {YELLOW}[..]{RESET} 正在后台静默启动 GPT-SoVITS API 服务...")
            proc = launch_gpt_sovits_daemon(sovits_dir)
            if proc:
                (PROJECT_ROOT / "gptsovits.pid").write_text(str(proc.pid), encoding="utf-8")
                print(f"      {GREEN}[OK]{RESET} 已启动 GPT-SoVITS 守护进程 (PID: {proc.pid})")
        else:
            print(f"      {YELLOW}[提示]{RESET} 未自动定位到 GPT-SoVITS 目录，如果已在其他位置启动可忽略。")

    # 3. Check and Launch Galgame2Voice (8080)
    print(f"{CYAN}[2/3] 正在启动 Galgame2Voice 后端服务 (端口 8080)...{RESET}")
    if is_port_in_use(8080):
        print(f"      {GREEN}[OK]{RESET} Galgame2Voice 服务已在运行 (端口 8080)")
    else:
        runner_script = PROJECT_ROOT / "scripts" / "run_server.py"
        cmd = [
            sys.executable,
            str(runner_script),
        ]
        flags = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            if sys.platform == "win32"
            else 0
        )

        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        (PROJECT_ROOT / "galgame2voice.pid").write_text(str(proc.pid), encoding="utf-8")
        print(f"      {GREEN}[OK]{RESET} Galgame2Voice 后台服务已拉起 (PID: {proc.pid})")

    # 4. Wait for server to become responsive & open browser
    print(f"{CYAN}[3/3] 正在等待就绪并打开浏览器...{RESET}")
    ready = False
    import urllib.request
    for _ in range(25):
        if is_port_in_use(8080):
            try:
                with urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=1) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                ready = True
                break
        time.sleep(0.3)

    if ready:
        url = "http://127.0.0.1:8080/"
        print(f"\n{GREEN}{BOLD}{'='*68}{RESET}")
        print(f"{GREEN}{BOLD}  🎉 启动成功！正在为您自动打开伴侣主页: {url}{RESET}")
        print(f"{GREEN}{BOLD}  若需停止所有服务，直接双击运行「停止.bat」即可。{RESET}")
        print(f"{GREEN}{BOLD}{'='*68}{RESET}\n")
        webbrowser.open(url)
    else:
        print(f"\n{RED}[WARN] 服务正在启动中，请在浏览器中手动访问: http://127.0.0.1:8080/{RESET}\n")


def stop_service():
    """Main stop orchestrator."""
    os.system("")
    print(f"\n{YELLOW}{BOLD}{'='*68}{RESET}")
    print(f"{YELLOW}{BOLD}        🛑 正在安全停止 Galgame2Voice 与 GPT-SoVITS 服务...{RESET}")
    print(f"{YELLOW}{BOLD}{'='*68}{RESET}\n")

    print(f"{CYAN}[1/2] 正在关闭 Galgame2Voice (8080 端口)...{RESET}")
    kill_processes_on_port(8080)
    pid_file = PROJECT_ROOT / "galgame2voice.pid"
    if pid_file.exists():
        try:
            saved_pid = pid_file.read_text(encoding="utf-8").strip()
            if saved_pid and sys.platform == "win32":
                subprocess.run(
                    f"taskkill /f /t /pid {saved_pid}",
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass
        pid_file.unlink(missing_ok=True)

    print(f"{CYAN}[2/2] 正在关闭 GPT-SoVITS (9880 端口)...{RESET}")
    kill_processes_on_port(9880)
    sovits_pid = PROJECT_ROOT / "gptsovits.pid"
    if sovits_pid.exists():
        try:
            saved_sovits_pid = sovits_pid.read_text(encoding="utf-8").strip()
            if saved_sovits_pid and sys.platform == "win32":
                subprocess.run(
                    f"taskkill /f /t /pid {saved_sovits_pid}",
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass
        sovits_pid.unlink(missing_ok=True)

    print(f"\n{GREEN}{BOLD}{'='*68}{RESET}")
    print(f"{GREEN}{BOLD}  🎉 全部后台服务已安全退出，端口与 GPU 显存已彻底释放！{RESET}")
    print(f"{GREEN}{BOLD}{'='*68}{RESET}\n")


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    if action == "stop":
        stop_service()
    elif action == "restart":
        stop_service()
        time.sleep(1)
        start_service()
    else:
        start_service()


if __name__ == "__main__":
    main()

"""Frozen Windows entry point; no Python installation is required by the user."""

import multiprocessing
import os
import sys

from galgame2voice.runtime_paths import get_install_root, low_disk_hint


def verify_portable_runtime() -> None:
    """Exercise bundled native libraries and the app's real FFmpeg subprocess path."""
    import asyncio
    import io
    import tkinter
    import wave

    from galgame2voice.utils.audio_converter import convert_wav_to_ogg, find_ffmpeg

    if tkinter.Tcl().eval("expr {1 + 1}") != "2":
        raise RuntimeError("Bundled Tcl runtime failed its self-check")
    exe_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    expected = get_install_root() / "tools" / exe_name
    if not find_ffmpeg() or os.path.normcase(find_ffmpeg()) != os.path.normcase(str(expected)):
        raise RuntimeError("Self-check must use the bundled FFmpeg executable")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)
    converted = asyncio.run(convert_wav_to_ogg(buffer.getvalue()))
    if not converted.startswith(b"OggS"):
        raise RuntimeError("Bundled FFmpeg did not produce valid OGG audio")
    print("Portable native runtime passed: Tcl and application FFmpeg conversion.")


def main() -> int:
    multiprocessing.freeze_support()
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    root = get_install_root()
    instance = None
    try:
        os.chdir(root)
        os.environ["GALGAME2VOICE_PROJECT_ROOT"] = str(root)
        if sys.argv[1:] == ["--restore-database"]:
            from scripts.desktop_instance import DesktopInstance
            from scripts.database_recovery import recover_interactively
            instance = DesktopInstance(root)
            if not instance.acquire():
                print("程序仍在运行。请先关闭 Galgame2Voice 的启动窗口，再双击「数据恢复.bat」。")
                return 1
            return recover_interactively()
        if getattr(sys, "frozen", False) and not (root / "tools" / "ffmpeg.exe").is_file():
            raise RuntimeError("缺少 tools/ffmpeg.exe，请重新完整解压便携包。")
        if sys.argv[1:] == ["--portable-check"]:
            verify_portable_runtime()
            return 0
        if getattr(sys, "frozen", False) and not any(arg in sys.argv for arg in ("--check-only", "--help", "-h")):
            # 全部解压缩 adds a folder named after the zip, so the old install can also be two levels up.
            old = [folder for folder in (root.parent, root.parent.parent) if folder != root
                   and (folder / "Galgame2Voice.exe").is_file() and (folder / "data" / "galgame2voice.db").is_file()]
            if old and not (root / "data" / "galgame2voice.db").exists():
                print(
                    f"[无法启动] 新版被解压到了旧版的 Galgame2Voice 文件夹里面（现在这个文件夹是 {root}），"
                    "所以读不到旧版的聊天记录和设置。请关闭这个窗口，把这个文件夹整个移到旧版文件夹外面（例如桌面），"
                    "再把旧版文件夹里的 data、audio、characters、backgrounds 文件夹复制进去，然后双击它里面的「启动.bat」。"
                )
                return 1
            from scripts.desktop_instance import DesktopInstance, open_existing_instance
            instance = DesktopInstance(root)
            if not instance.acquire():
                no_browser = "--no-browser" in sys.argv or os.environ.get("GALGAME_NO_BROWSER", "").lower() in ("1", "true", "yes")
                open_existing_instance(root, no_browser=no_browser)
                return 0
            (root / "data" / "desktop_instance.json").unlink(missing_ok=True)
        from scripts.run_server import main as run
        run()
    except SystemExit as exc:
        if isinstance(exc.code, str) and exc.code:
            print(exc.code, file=sys.stderr)
        return exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:
        print(f"[启动失败] {exc}")
        print(low_disk_hint(root) or "请将完整便携包解压到有写入权限的目录，并保留 _internal 文件夹。")
        try:
            from datetime import datetime, timezone
            import traceback
            log = root / "logs" / "launcher_error.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as handle:
                handle.write(f"\n{datetime.now(timezone.utc).isoformat()}\n")
                traceback.print_exc(file=handle)
            print(f"详细错误已保存：{log}")
        except OSError:
            print("无法写入错误日志，请检查解压目录的写入权限。")
        return 1
    finally:
        if instance is not None:
            instance.close()
    return 0


if __name__ == "__main__":
    result = main()
    if result and sys.stdin.isatty() and not any(arg in sys.argv for arg in ("--check-only", "--help")):
        input("按回车关闭窗口……")
    raise SystemExit(result)

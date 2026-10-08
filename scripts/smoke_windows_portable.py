"""Exercise the actual release ZIP with no Python/FFmpeg in the child's PATH."""

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import wave
import zipfile


def smoke_test(archive: Path, scratch: Path) -> None:
    scratch.mkdir(parents=True, exist_ok=True)
    with archive.open("rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        actual_hash = digest.hexdigest()
    expected_hash = archive.with_suffix(".zip.sha256").read_text().split()[0]
    if actual_hash != expected_hash:
        raise RuntimeError("Release checksum does not match")
    with tempfile.TemporaryDirectory(prefix="portable 中文 ! ", dir=scratch) as temporary:
        root = Path(temporary).resolve()
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                (root / entry.filename).resolve().relative_to(root)
            bundle.extractall(root)
        install = root / "Galgame2Voice"
        executable = install / "Galgame2Voice.exe"
        ffmpeg = install / "tools" / "ffmpeg.exe"
        env = {key: value for key, value in os.environ.items() if key.upper() in {
            "SYSTEMROOT", "WINDIR", "COMSPEC", "PROGRAMDATA", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        }}
        env.update({
            "PATH": str(Path(os.environ["SystemRoot"]) / "System32"),
            "TEMP": str(root), "TMP": str(root),
            "GALGAME2VOICE_GPT_SOVITS_BASE_URL": "http://127.0.0.1:1",
            "GALGAME2VOICE_AUTH_DISABLED": "1",
        })
        dependency_check = subprocess.run(
            [str(executable), "--check-only"], cwd=root, env=env,
            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if dependency_check.returncode:
            raise RuntimeError(f"Portable dependency check failed: {dependency_check.stdout!r} {dependency_check.stderr!r}")
        wrapper_check = subprocess.run(
            [env.get("COMSPEC", "cmd.exe"), "/d", "/v:off", "/c", "call", str(install / "启动.bat"), "--check-only"],
            cwd=root, env=env, capture_output=True, input=b"\n", timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if wrapper_check.returncode:
            raise RuntimeError(f"Portable launcher wrapper failed: {wrapper_check.stdout!r} {wrapper_check.stderr!r}")
        native_check = subprocess.run(
            [str(executable), "--portable-check"], cwd=root, env=env,
            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if native_check.returncode:
            raise RuntimeError(f"Portable native self-check failed: {native_check.stdout!r} {native_check.stderr!r}")
        disabled_ffmpeg = ffmpeg.with_suffix(".disabled")
        ffmpeg.rename(disabled_ffmpeg)
        try:
            incomplete = subprocess.run(
                [str(executable), "--check-only"], cwd=root, env=env, capture_output=True,
                timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            error_log = install / "logs" / "launcher_error.log"
            if incomplete.returncode == 0 or not error_log.is_file():
                raise RuntimeError("An incomplete portable package did not leave a startup diagnostic")
            if "ffmpeg.exe" not in error_log.read_text(encoding="utf-8"):
                raise RuntimeError("Startup diagnostic did not identify the missing FFmpeg executable")
        finally:
            disabled_ffmpeg.rename(ffmpeg)
        subprocess.run([str(ffmpeg), "-version"], env=env, check=True, capture_output=True, timeout=10)
        reference = root / "reference.wav"
        with wave.open(str(reference), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * 16000)
        subprocess.run([
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(reference),
            "-c:a", "libopus", str(root / "converted.ogg"),
        ], env=env, check=True, capture_output=True, timeout=15)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with (root / "launch.log").open("wb") as log:
            process = subprocess.Popen(
                [str(executable), "--no-engine", "--no-browser", "--port", str(port)],
                cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Portable application exited early")
                    try:
                        with opener.open(base_url + "/api/health", timeout=2) as response:
                            if response.status == 200:
                                break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.25)
                else:
                    raise RuntimeError("Portable application did not become ready")
                instance_file = install / "data" / "desktop_instance.json"
                before_duplicate = instance_file.read_bytes()
                duplicate = subprocess.run(
                    [str(executable), "--no-engine", "--no-browser", "--port", "1"],
                    cwd=root, env=env, capture_output=True, timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if duplicate.returncode or process.poll() is not None:
                    raise RuntimeError("A duplicate launch failed or stopped the original server")
                if instance_file.read_bytes() != before_duplicate:
                    raise RuntimeError("A duplicate launch replaced the original server's address")
                if str(port).encode() not in duplicate.stdout:
                    raise RuntimeError("A duplicate launch did not report the original server's port")
                recovery_while_running = subprocess.run(
                    [str(executable), "--restore-database"], cwd=root, env=env,
                    capture_output=True, input=b"\n", timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if recovery_while_running.returncode == 0 or "程序仍在运行" not in recovery_while_running.stdout.decode("utf-8"):
                    raise RuntimeError("Database recovery did not reject a running desktop instance")
                with opener.open(base_url + "/", timeout=5) as response:
                    html = response.read().decode("utf-8")
                asset = re.search(r'src="(/static/assets/[^\"]+\.js)"', html)
                if not asset:
                    raise RuntimeError("Portable frontend HTML does not reference a built script")
                with opener.open(base_url + asset[1], timeout=5) as response:
                    if "javascript" not in response.headers.get("Content-Type", "") or len(response.read()) < 100:
                        raise RuntimeError("Portable frontend script is missing or has the wrong MIME type")
                browser_candidates = [
                    Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
                    Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
                ]
                browser = next((candidate for candidate in browser_candidates if candidate.is_file()), None)
                if browser:
                    # The host may already isolate child processes. This browser
                    # only visits our temporary loopback fixture with fake data.
                    rendered = subprocess.run([
                        str(browser), "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-sync",
                        "--no-proxy-server",
                        "--disable-background-networking", "--dump-dom", "--virtual-time-budget=5000",
                        f"--user-data-dir={root / 'browser-profile'}", base_url,
                    ], env=env, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
                    if rendered.returncode or "支持语音输入与朗读回复" not in rendered.stdout.decode("utf-8", errors="replace"):
                        (scratch / "browser-rendered.html").write_bytes(rendered.stdout)
                        (scratch / "browser-errors.log").write_bytes(rendered.stderr)
                        print(f"Browser exit status: {rendered.returncode}; diagnostics saved in {scratch}")
                        raise RuntimeError("The real browser did not initialize the portable chat interface")
                    print("Portable browser initialization passed with an isolated headless profile.")
                with opener.open(base_url + "/api/system/engine-status", timeout=10) as response:
                    initial_engine = json.load(response)
                    if initial_engine["ready"] or initial_engine["state"] != "unavailable":
                        raise RuntimeError("A missing engine was incorrectly reported ready")
                with opener.open(base_url + "/api/voice/profiles", timeout=5) as response:
                    if json.load(response)["profiles"] != []:
                        raise RuntimeError("Portable release unexpectedly includes a user's voice profiles")
                if 'id="gSovitsDirectory"' not in html:
                    raise RuntimeError("Portable frontend does not include the engine directory settings")
                with opener.open(base_url + "/api/system/sovits-directory", timeout=5) as response:
                    if json.load(response)["directory"] != "":
                        raise RuntimeError("Portable release unexpectedly includes a remembered engine directory")
                # Only validate directory markers; never run this fake engine.
                engine = root / "selected engine 中文 !"
                (engine / "runtime").mkdir(parents=True)
                (engine / "api_v2.py").write_text("# smoke marker, never executed\n")
                (engine / "runtime" / "python.exe").write_bytes(b"smoke marker, never executed")
                (engine / "GPT_weights").mkdir()
                weight = engine / "GPT_weights" / "smoke.ckpt"
                weight.write_bytes(b"smoke marker, never executed")
                directory_request = urllib.request.Request(
                    base_url + "/api/system/sovits-directory", method="PUT",
                    data=json.dumps({"directory": str(engine)}).encode(),
                    headers={"Content-Type": "application/json", "Origin": base_url},
                )
                with opener.open(directory_request, timeout=5) as response:
                    if json.load(response)["directory"] != str(engine):
                        raise RuntimeError("Portable engine directory was not persisted")
                with opener.open(base_url + "/api/voice/scan-models", timeout=10) as response:
                    if str(weight) not in {item["path"] for item in json.load(response)["gpt_weights"]}:
                        raise RuntimeError("Portable model discovery did not use the selected engine")
                stored = (install / "data" / "sovits_dir.txt").read_text(encoding="utf-8")
                if not stored.startswith(".."):
                    raise RuntimeError("Sibling engine directory was not stored portably")
                # A saved directory can become incomplete; reject it before spawning.
                runtime_marker = engine / "runtime" / "python.exe"
                runtime_marker.unlink()
                restart_request = urllib.request.Request(
                    base_url + "/api/system/restart_sovits", method="POST", data=b'{}',
                    headers={"Content-Type": "application/json", "Origin": base_url},
                )
                try:
                    opener.open(restart_request, timeout=10)
                    raise RuntimeError("An incomplete engine unexpectedly started")
                except urllib.error.HTTPError as failure:
                    if failure.code != 400 or "完整集成包" not in json.load(failure)["detail"]:
                        raise RuntimeError("Incomplete engine guidance was missing") from failure
                finally:
                    runtime_marker.write_bytes(b"smoke marker, never executed")
                with opener.open(base_url + "/api/system/version", timeout=5) as response:
                    if json.load(response)["current_branch"] != "portable":
                        raise RuntimeError("Portable version detection used a source Git checkout")
                update_request = urllib.request.Request(
                    base_url + "/api/system/update/apply", method="POST",
                    data=b'{"discard_local_changes":true}',
                    headers={"Content-Type": "application/json", "Origin": base_url},
                )
                with opener.open(update_request, timeout=5) as response:
                    result = json.load(response)
                    if result["success"] or "使用说明" not in result["output"]:
                        raise RuntimeError("Portable update did not direct users to the release ZIP")
                request = urllib.request.Request(
                    base_url + "/api/providers/deepseek", method="PUT",
                    data=json.dumps({"api_key": "portable-test-credential"}).encode(),
                    headers={"Content-Type": "application/json", "Origin": base_url},
                )
                with opener.open(request, timeout=5) as response:
                    response.read()
                with closing(sqlite3.connect(install / "data" / "galgame2voice.db")) as connection:
                    value = connection.execute("SELECT api_key FROM providers WHERE id = 'deepseek'").fetchone()[0]
                    if not value.startswith(("dpapi:", "enc:")):
                        raise RuntimeError("Portable credentials were not encrypted")
                if (install / "_internal" / "data").exists():
                    raise RuntimeError("User data was written inside the bundled runtime")
                with opener.open(base_url + "/api/providers/deepseek", timeout=5) as response:
                    if json.load(response)["provider"]["api_key"] != "por****tial":
                        raise RuntimeError("Portable credentials could not be decrypted for display")
                import_request = urllib.request.Request(
                    base_url + "/api/chat/import", method="POST",
                    data=json.dumps({"sessions": [{"id": "s_import_portable_smoke", "title": "Imported chat",
                        "settings": {"voiceProfileId": 999}, "messages": [
                            {"role": "user", "content": "Retained history after import"}]}]}).encode(),
                    headers={"Content-Type": "application/json", "Origin": base_url},
                )
                with opener.open(import_request, timeout=10) as response:
                    if json.load(response)["count"] != 1:
                        raise RuntimeError("Portable chat import failed")
                with opener.open(base_url + "/api/chat/history?session_id=s_import_portable_smoke", timeout=5) as response:
                    if json.load(response)["messages"][0]["content_chinese"] != "Retained history after import":
                        raise RuntimeError("Portable chat import did not restore model context")
            except Exception:
                log.flush()
                print((root / "launch.log").read_text(encoding="utf-8", errors="replace"))
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
        # Exercise the documented update: new extraction plus user folders and .env.
        # Both release and engine move together, so their relative hint stays valid.
        (install / ".env").write_text("# personal settings smoke marker\n", encoding="utf-8")
        for name in ("audio", "characters"):
            (install / name / "user-note.txt").write_text("retained user file", encoding="utf-8")
        updated_root = root / "updated release 中文 !"
        updated_root.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                (updated_root / entry.filename).resolve().relative_to(updated_root.resolve())
            bundle.extractall(updated_root)
        updated = updated_root / "Galgame2Voice"
        for name in ("data", "audio", "characters"):
            shutil.copytree(install / name, updated / name, dirs_exist_ok=True)
        shutil.copy2(install / ".env", updated / ".env")
        moved_engine = updated_root / engine.name
        shutil.copytree(engine, moved_engine)
        with (root / "updated-launch.log").open("wb") as log:
            process = subprocess.Popen(
                [str(updated / "Galgame2Voice.exe"), "--no-engine", "--no-browser", "--port", str(port)],
                cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Updated portable application exited early")
                    try:
                        with opener.open(base_url + "/api/health", timeout=2) as response:
                            if response.status == 200:
                                break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.25)
                else:
                    raise RuntimeError("Updated portable application did not become ready")
                with opener.open(base_url + "/api/providers/deepseek", timeout=5) as response:
                    if json.load(response)["provider"]["api_key"] != "por****tial":
                        raise RuntimeError("Updated release could not read the preserved encrypted credential")
                with opener.open(base_url + "/api/system/sovits-directory", timeout=5) as response:
                    if json.load(response)["directory"] != str(moved_engine):
                        raise RuntimeError("Updated release lost the sibling engine directory")
                with opener.open(base_url + "/api/voice/scan-models", timeout=10) as response:
                    moved_weight = moved_engine / "GPT_weights" / "smoke.ckpt"
                    if str(moved_weight) not in {item["path"] for item in json.load(response)["gpt_weights"]}:
                        raise RuntimeError("Updated release could not find the relocated model")
                for name in ("audio", "characters"):
                    if (updated / name / "user-note.txt").read_text(encoding="utf-8") != "retained user file":
                        raise RuntimeError(f"Updated release lost the user's {name} files")
                if (updated / ".env").read_bytes() != (install / ".env").read_bytes():
                    raise RuntimeError("Updated release replaced the personal .env")
            except Exception:
                log.flush()
                print((root / "updated-launch.log").read_text(encoding="utf-8", errors="replace"))
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
        # All data here belongs to the smoke fixture. A corrupt database must
        # fail visibly, without being silently erased or reported as normal exit.
        broken_database = updated / "data" / "galgame2voice.db"
        broken_database.resolve().relative_to(root)
        for suffix in ("-wal", "-shm"):
            broken_database.with_name(broken_database.name + suffix).unlink(missing_ok=True)
        broken_database.write_bytes(b"smoke fixture: intentionally invalid SQLite database")
        failed_start = subprocess.run(
            [str(updated / "Galgame2Voice.exe"), "--no-engine", "--no-browser", "--port", str(port)],
            cwd=root, env=env, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if failed_start.returncode == 0:
            raise RuntimeError("An application startup failure was incorrectly reported successful")
        recovery = subprocess.run(
            [str(updated / "Galgame2Voice.exe"), "--restore-database"], cwd=root, env=env,
            capture_output=True, input="1\n恢复\n".encode("utf-8"), timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if recovery.returncode or "恢复完成" not in recovery.stdout.decode("utf-8", errors="replace"):
            raise RuntimeError(f"Portable database recovery failed: {recovery.stdout!r} {recovery.stderr!r}")
        preserved = list((updated / "data" / "backups").glob("before_restore_*/galgame2voice.db"))
        if len(preserved) != 1 or preserved[0].read_bytes() != b"smoke fixture: intentionally invalid SQLite database":
            raise RuntimeError("Portable recovery did not preserve the damaged original")
        with closing(sqlite3.connect(broken_database)) as connection:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RuntimeError("Portable recovery produced an invalid database")
            if connection.execute("SELECT content_chinese FROM messages WHERE session_id='s_import_portable_smoke'").fetchone() != ("Retained history after import",):
                raise RuntimeError("Portable recovery lost the imported chat history")
        with (root / "recovered-launch.log").open("wb") as log:
            process = subprocess.Popen(
                [str(updated / "Galgame2Voice.exe"), "--no-engine", "--no-browser", "--port", str(port)],
                cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Recovered portable application exited early")
                    try:
                        with opener.open(base_url + "/api/health", timeout=2) as response:
                            if response.status == 200:
                                break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.25)
                else:
                    raise RuntimeError("Recovered portable application did not become ready")
                with opener.open(base_url + "/api/providers/deepseek", timeout=5) as response:
                    if json.load(response)["provider"]["api_key"] != "por****tial":
                        raise RuntimeError("Restored application could not read its preserved credential")
            except Exception:
                log.flush()
                print((root / "recovered-launch.log").read_text(encoding="utf-8", errors="replace"))
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
        print("Portable smoke test passed: restricted PATH, Unicode/spaces/!, Tcl, HTML/JS, first-install database, encrypted credentials, application FFmpeg conversion, engine directory/model discovery, startup diagnostics, duplicate launch protection, update data preservation and relocation, missing-engine recovery guidance, nonzero startup failure, atomic chat import, offline database recovery with original preservation and live-instance protection.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--scratch", type=Path, default=Path("temp_test/portable-smoke"))
    arguments = parser.parse_args()
    smoke_test(arguments.archive.resolve(), arguments.scratch.resolve())

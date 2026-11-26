"""Saving the engine folder blames the right thing when the file system says no.

A folder that cannot be checked (a disconnected network drive, no access, a name
that is too long) used to come back as a 500 asking the user to fix the program
folder's write permission. Only a failure writing data/sovits_dir.txt is that.
"""

import errno
from pathlib import Path

from httpx import ASGITransport, AsyncClient
import pytest

from galgame2voice.config import get_settings
from galgame2voice.main import create_app


async def _save(directory: str):
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8080") as client:
        return await client.put("/api/system/sovits-directory", json={"directory": directory})


@pytest.mark.asyncio
@pytest.mark.parametrize("parts", [("nas", "GPT-SoVITS"), ("app", "data", "GPT-SoVITS")])
async def test_unreadable_chosen_folder_is_a_400_about_that_folder(tmp_path, monkeypatch, parts):
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "project_root", root)
    chosen = tmp_path.joinpath(*parts)
    target = chosen.resolve()
    real_is_dir = Path.is_dir

    def refuse(self, *args, **kwargs):
        if self == target:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_is_dir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", refuse)
    result = await _save(str(chosen))
    assert result.status_code == 400
    assert "无法打开所选文件夹" in result.json()["detail"]
    assert "写入权限" not in result.json()["detail"]


@pytest.mark.asyncio
async def test_failure_writing_the_hint_still_asks_for_write_permission(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    (root / "data").write_text("not a folder", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "project_root", root)
    engine = tmp_path / "GPT-SoVITS"
    engine.mkdir()
    (engine / "api_v2.py").write_text("# marker\n", encoding="utf-8")
    result = await _save(str(engine))
    assert result.status_code == 500
    assert result.json()["detail"] == "无法保存引擎目录，请确认程序目录有写入权限。"

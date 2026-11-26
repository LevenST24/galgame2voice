"""A data folder moved to another computer or Windows user keeps a working access token.

On Windows the token is saved with DPAPI, which only the account that saved it can read.
After the folder moves, the stored token reads back empty, so the token in
data/.console_token never matched again and the page asked for it forever.
"""

import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import get_db, init_db
from galgame2voice.security.crypto import decrypt_secret

# Valid base64 that no account can unprotect: reads back empty, as on another computer.
UNREADABLE = "dpapi:Z2FsZ2FtZTJ2b2ljZQ=="


async def _store_unreadable_token() -> None:
    async with get_db() as conn:
        await conn.execute("UPDATE settings SET console_token = ? WHERE id = 1;", (UNREADABLE,))
        await conn.commit()


async def _stored_token() -> str:
    async with get_db() as conn:
        row = await (await conn.execute("SELECT console_token FROM settings WHERE id = 1;")).fetchone()
    return str(row[0])


@pytest.mark.asyncio
async def test_the_token_in_the_file_works_again_after_a_move():
    token_file = get_settings().data_dir / ".console_token"
    token_file.write_text("moved-folder-token-1234\n", encoding="utf-8")
    await _store_unreadable_token()

    await init_db()

    assert decrypt_secret(await _stored_token()) == "moved-folder-token-1234"
    async with get_db() as conn:
        assert await crud.verify_console_token(conn, "moved-folder-token-1234")
    assert token_file.read_text(encoding="utf-8").strip() == "moved-folder-token-1234"


@pytest.mark.asyncio
async def test_without_the_file_a_new_token_is_made_and_written():
    token_file = get_settings().data_dir / ".console_token"
    token_file.unlink(missing_ok=True)
    await _store_unreadable_token()

    await init_db()

    new_token = token_file.read_text(encoding="utf-8").strip()
    assert new_token
    async with get_db() as conn:
        assert await crud.verify_console_token(conn, new_token)


@pytest.mark.asyncio
async def test_a_readable_token_is_left_alone():
    token_file = get_settings().data_dir / ".console_token"
    token_file.write_text("some-other-text", encoding="utf-8")
    before = await _stored_token()
    readable = decrypt_secret(before)
    assert readable

    await init_db()

    assert await _stored_token() == before
    assert token_file.read_text(encoding="utf-8") == "some-other-text"

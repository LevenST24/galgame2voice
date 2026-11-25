"""An out-of-range number such as 1e400 in tts_options is a 422, not a 500.

JSON numbers like 1e400 parse to float infinity; int(inf) raises OverflowError,
which the boundary validator used to let through as an internal server error.
"""

import json

import httpx
import pytest

from galgame2voice.main import create_app
from galgame2voice.services.tts_options import validate_user_tts_options


@pytest.mark.parametrize("key", ["top_k", "batch_size", "seed", "voice_profile_id"])
@pytest.mark.parametrize("raw", ["1e400", "-1e400"])
def test_infinite_integer_option_is_a_value_error(key, raw):
    options = json.loads(f'{{"{key}": {raw}}}')
    with pytest.raises(ValueError):
        validate_user_tts_options(options)


@pytest.mark.asyncio
async def test_chat_rejects_huge_top_k_with_422(isolate_test_database):
    app = create_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        resp = await client.post(
            "/api/chat/stream",
            content=b'{"prompt": "hi", "tts_options": {"top_k": 1e400}}',
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 422
    assert "top_k" in resp.text

"""The 音频语言 choice for a custom voice reaches GPT-SoVITS as prompt_lang.

The settings page sends prompt_lang only; the unset refer_language default ("ja")
used to win over it, so a Chinese or English reference clip was described as Japanese.
"""
from httpx import ASGITransport, AsyncClient

from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from galgame2voice.main import create_app


async def test_chinese_reference_audio_keeps_prompt_lang_zh():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        # Exactly the payload shape the settings UI sends when 音频语言 = 中文
        resp = await client.post("/api/voice/profiles", json={
            "name": "中文参考",
            "gpt_weights_path": "weights/original.ckpt",
            "sovits_weights_path": "weights/original.pth",
            "ref_audio_path": "audio/original.wav",
            "prompt_text": "今天天气真不错",
            "prompt_lang": "zh",
            "text_lang": "zh",
            "system_prompt": "",
        })
        assert resp.status_code == 201, resp.text
        async with get_db() as conn:
            profile = await crud.get_voice_profile(conn, resp.json()["id"])
    # The reference transcript is Chinese, so GPT-SoVITS must get prompt_lang=zh.
    assert profile.prompt_lang == "zh", f"stored prompt_lang={profile.prompt_lang!r}"


async def test_legacy_refer_language_is_still_honoured():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        resp = await client.post("/api/voice/profiles", json={
            "name": "旧字段",
            "gpt_weights_path": "weights/original.ckpt",
            "sovits_weights_path": "weights/original.pth",
            "refer_audio_path": "audio/original.wav",
            "refer_text": "Hello there",
            "refer_language": "en",
        })
        assert resp.status_code == 201, resp.text
        async with get_db() as conn:
            profile = await crud.get_voice_profile(conn, resp.json()["id"])
    assert profile.prompt_lang == "en"
    assert profile.text_lang == "ja"

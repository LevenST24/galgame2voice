import json
import httpx
import pytest
from galgame2voice.services.chat_pipelines.tts_pipeline import TtsStreamPipeline
from galgame2voice.services.gpt_sovits_client import GptSovitsClient, NON_AUDIO_RESPONSE_ERROR, describe_tts_synthesis_error
from galgame2voice.services.tts_service import TtsService
from tests.test_line_audio_regeneration import repair  # noqa: F401

@pytest.mark.asyncio
@pytest.mark.parametrize("exc_text,expected", [
    ("CUDA out of memory. Tried to allocate 512 MiB", "显存不足"),
    ("Reference audio is outside the 3-10 second range, please choose another one!", "3–10 秒"),
    ("Error opening 'missing_ref.wav': System error.", "找不到"),
])
async def test_chat_tts_pipeline_surfaces_sovits_api_error(tmp_path, exc_text, expected):
    for name in ("a.ckpt", "a.pth", "ref.wav"):
        (tmp_path / name).write_bytes(b"stub")
    body = json.dumps({"message": "tts failed", "Exception": exc_text})
    class _Srv:
        async def handle_request(self, method, path, params=None, json_data=None):
            return httpx.Response(400, text=body) if path == "/tts" else httpx.Response(200, json={"message": "ok"})
    client = GptSovitsClient(server=_Srv())
    svc = TtsService(client=client, db_path=None)
    profile = {"id": 1, "name": "T", "gpt_weights_path": str(tmp_path / "a.ckpt"),
               "sovits_weights_path": str(tmp_path / "a.pth"), "ref_audio_path": str(tmp_path / "ref.wav"), "prompt_text": "hi"}
    assert await client.switch_voice_profile(profile, force=True)
    url, err = await TtsStreamPipeline(tts_service=svc, generation_id="g1").synthesize_chunk("こんにちは。", 0, dict(profile))
    assert url is None and expected in err and "status 400" not in err

@pytest.mark.asyncio
@pytest.mark.parametrize("err_text,expected", [
    ('TTS synthesis failed with status 400: {"message":"tts failed","Exception":"CUDA out of memory"}', "显存不足"),
    ('TTS synthesis failed with status 400: {"message":"tts failed","Exception":"Reference audio is outside the 3-10 second range"}', "3–10 秒"),
    (NON_AUDIO_RESPONSE_ERROR, "不是音频"),
])
async def test_regenerate_line_surfaces_tts_api_error(repair, monkeypatch, err_text, expected):  # noqa: F811
    monkeypatch.setattr(repair.service, "synthesize_to_file", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(err_text)))
    resp = await repair.client.post("/api/voice/regenerate-line", json=repair.body)
    assert resp.status_code == 502 and expected in resp.json()["detail"] and "原音频与文字已保留" in resp.json()["detail"]
    assert describe_tts_synthesis_error(OSError(2, "No such file or directory")) is None and describe_tts_synthesis_error(OSError(28, "No space left on device")) is None

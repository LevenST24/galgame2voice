import json
import httpx
import pytest
from fastapi import HTTPException
from galgame2voice.routers.common import switch_voice_profile_or_raise
from galgame2voice.services.audio_identity import VoicePreparation
from galgame2voice.services.gpt_sovits_client import GptSovitsClient, describe_sovits_api_error
from galgame2voice.services.voice_manager import VoiceManager

class _ErrorSovitsServer:
    def __init__(self, fail_path: str, payload: dict):
        self.fail_path, self.payload = fail_path, payload

    async def handle_request(self, method: str, path: str, params=None, json_data=None):
        if path == self.fail_path:
            return httpx.Response(400, text=json.dumps(self.payload, ensure_ascii=False))
        return httpx.Response(200, json={"message": "success"})

@pytest.mark.asyncio
@pytest.mark.parametrize("fail_path,exc_text,expected", [
    ("/set_gpt_weights", "CUDA out of memory. Tried to allocate 1.2 GiB", "显存不足"),
    ("/set_sovits_weights", "Error(s) in loading state_dict for SynthesizerTrn: size mismatch", "版本不匹配"),
    ("/set_refer_audio", "Reference audio is outside the 3-10 second range, please choose another one!", "3–10 秒"),
    ("/set_refer_audio", "[Errno 2] No such file or directory: 'ref.wav'", "找不到"),
])
async def test_switch_error_surfaces_actionable_chinese_guidance(tmp_path, fail_path, exc_text, expected):
    gpt, sovits, ref = tmp_path / "a.ckpt", tmp_path / "a.pth", tmp_path / "ref.wav"
    for p in (gpt, sovits, ref):
        p.write_bytes(b"stub")
    profile = {"id": 1, "name": "Test", "gpt_weights_path": str(gpt),
               "sovits_weights_path": str(sovits), "ref_audio_path": str(ref), "prompt_text": "hi"}
    client = GptSovitsClient(server=_ErrorSovitsServer(fail_path, {"message": "failed", "Exception": exc_text}))
    manager = VoiceManager(gpt_sovits_client_or_server=client, db_path=str(tmp_path / "db.sqlite"))
    with pytest.raises(HTTPException) as err:
        await switch_voice_profile_or_raise(manager, profile, force=True)
    assert err.value.status_code == 502 and expected in err.value.detail
    with pytest.raises(ValueError) as synth_err:
        await client.synthesize("こんにちは", {**profile, "voice_profile_id": 1, "_voice_preparation": VoicePreparation(name="Test")})
    assert expected in str(synth_err.value)


def test_unrelated_errno_is_not_reported_as_missing_file():
    body = json.dumps({"message": "set refer audio failed", "Exception": "[Errno 28] No space left on device"})
    assert describe_sovits_api_error(body) is None

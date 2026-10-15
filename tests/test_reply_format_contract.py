"""Every persona prompt carries the bilingual reply format the voice pipeline depends on."""

import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.chat_pipelines.context_builder import (
    BILINGUAL_OUTPUT_CONTRACT,
    ensure_bilingual_output_format,
)
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.streaming_parser import StreamingBilingualParser

PACKAGED_PROMPT = (
    "你是夏目。\n你必须严格输出如下 JSON 格式：\n"
    '{"tts": {"speed": 1.0, "temp": 0.9, "emotion": "gentle"}, "chinese": "中文", "japanese": "日本語"}'
)


def test_contract_is_added_only_when_the_prompt_lacks_one():
    assert ensure_bilingual_output_format("你是爱丽丝，说话温柔。").endswith(BILINGUAL_OUTPUT_CONTRACT)
    assert ensure_bilingual_output_format(PACKAGED_PROMPT) == PACKAGED_PROMPT
    assert ensure_bilingual_output_format("") == ""


def test_the_contract_example_is_a_reply_the_parser_accepts():
    example = BILINGUAL_OUTPUT_CONTRACT[BILINGUAL_OUTPUT_CONTRACT.index("{"):]
    parser = StreamingBilingualParser()
    parser.feed_chunk(example)
    chinese, japanese, _ = parser.finalize()
    assert chinese == "给玩家看的中文内容"
    assert japanese == "对应的口语化日语音频台词"
    assert parser.tts_emotion == "gentle"


@pytest.mark.asyncio
async def test_hand_written_personas_get_the_format_and_packaged_ones_stay_unchanged(tmp_path):
    db_file = tmp_path / "contract.db"
    await init_db(db_file)
    async with get_db(str(db_file)) as conn:
        def profile(name, prompt):
            return VoiceProfileCreate(
                name=name, gpt_weights_path=f"{name}.ckpt", sovits_weights_path=f"{name}.pth",
                ref_audio_path=f"{name}.wav", prompt_text="テスト", prompt_lang="ja", text_lang="ja",
                system_prompt=prompt,
            )

        custom = await crud.create_voice_profile(conn, profile("爱丽丝", "你是爱丽丝，说话温柔。"))
        packaged = await crud.create_voice_profile(conn, profile("夏目", PACKAGED_PROMPT))
        service = ChatService(db_path=str(db_file))

        for prof, expected_contracts in ((custom, 1), (packaged, 0)):
            session = await crud.upsert_session(conn=conn, session_id=f"s{prof.id}", title="t", voice_profile_id=prof.id)
            messages = await service._prepare_messages(conn=conn, session_id=session.id, user_prompt="你好", session=session)
            system = messages[0].content
            assert messages[0].role == "system"
            assert system.count(BILINGUAL_OUTPUT_CONTRACT) == expected_contracts, prof.name
            assert prof.system_prompt.split("\n")[0] in system

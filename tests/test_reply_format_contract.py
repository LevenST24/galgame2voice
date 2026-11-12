"""Every persona prompt carries the per-line bilingual reply format the voice pipeline depends on."""

import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.chat_pipelines.line_scanner import LineScanner
from galgame2voice.services.chat_pipelines.reply_contract import render_lines_contract
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.streaming_parser import StreamingBilingualParser

PACKAGED_PROMPT = (
    "你是夏目。\n【输出契约与情绪控制】\n"
    "重要：你必须严格输出如下 JSON 格式，不要输出任何多余文字：\n"
    '{"tts": {"speed": 1.0, "temp": 0.9, "emotion": "gentle"}, '
    '"chinese": "显示给玩家的中文台词", "japanese": "对应的口语化日文台词"}'
)


def test_the_contract_example_is_a_reply_the_parser_and_scanner_accept():
    contract = render_lines_contract(None)
    example = contract.splitlines()[1]
    parser = StreamingBilingualParser()
    scanner = LineScanner()
    parser.feed_chunk(example)
    lines = scanner.feed(example)
    parser.finalize()
    result = scanner.finish()
    assert result.format == "lines"
    assert len(lines) == 1
    assert lines[0].zh == "给玩家看的中文台词"
    assert lines[0].ja == "意思相同的口语化日语台词"
    assert lines[0].emotion == "gentle"
    assert parser.tts_emotion == "gentle"


@pytest.mark.asyncio
async def test_hand_written_personas_and_legacy_packaged_prompts_receive_lines_contract(tmp_path):
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
        expected_contract = render_lines_contract(None)

        for prof in (custom, packaged):
            session = await crud.upsert_session(conn=conn, session_id=f"s{prof.id}", title="t", voice_profile_id=prof.id)
            for kwargs in ({}, {"reply_format": "old"}):
                messages = await service._prepare_messages(
                    conn=conn, session_id=session.id, user_prompt="你好", session=session, **kwargs
                )
                system = messages[0].content
                assert messages[0].role == "system"
                assert prof.system_prompt.split("\n")[0] in system
                assert expected_contract in system, prof.name
                assert system.count("【输出格式】") == 1, prof.name
                assert '"chinese"' not in system, prof.name

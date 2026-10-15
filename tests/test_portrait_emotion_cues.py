"""Per-sentence portrait emotions fall back to the reply's emotion, not to 'gentle'."""

import json

import pytest

from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.emotion_classifier import classify_emotion, detect_emotion_cue
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_service import TtsService
from tests.test_chat_streaming_m4 import MockStreamingLLMAdapter


def test_detect_emotion_cue_reports_only_signals_present_in_the_text():
    assert detect_emotion_cue("今日はいい天気ですね。") is None
    assert detect_emotion_cue("") is None
    assert detect_emotion_cue("ごめんなさい、遅れました。") == "sad"
    assert detect_emotion_cue("（害羞）那个……") == "shy"
    # classify_emotion keeps its contract: explicit > cue > 'gentle'.
    assert classify_emotion("今日はいい天気ですね。") == "gentle"
    assert classify_emotion("今日はいい天気ですね。", "", "happy") == "happy"
    assert classify_emotion("ごめんなさい。") == "sad"


@pytest.mark.asyncio
@pytest.mark.parametrize("adaptive", [True, False])
async def test_neutral_sentences_follow_the_declared_reply_emotion(
    adaptive, temp_db_path, mock_gpt_sovits, tmp_path, configured_voice,
):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    chat_service = ChatService(
        tts_service=TtsService(client=GptSovitsClient(server=mock_gpt_sovits), audio_dir=audio_dir),
        db_path=temp_db_path,
    )
    reply = json.dumps({
        "tts": {"speed": 1.1, "emotion": "happy"},
        "chinese": "今天天气真好。对不起，我来晚了。",
        "japanese": "今日はいい天気ですね。ごめんなさい、遅れました。",
    }, ensure_ascii=False)
    adapter = MockStreamingLLMAdapter(stream_chunks=[reply[i:i + 12] for i in range(0, len(reply), 12)])

    async def _adapter(*args, **kwargs):
        return adapter, "mock-chat"

    chat_service._get_active_llm_adapter = _adapter

    events = [event async for event in chat_service.stream_chat(
        prompt="今天怎么样", session_id=f"emotion-{adaptive}", ai_adaptive_voice=adaptive,
    )]
    chunks = [event["data"] for event in events if event["event"] == "audio_chunk"]
    by_sentence = {chunk["sentence"]: chunk["emotion"] for chunk in chunks}
    assert any("天気" in sentence for sentence in by_sentence), by_sentence
    for sentence, emotion in by_sentence.items():
        expected = "sad" if "ごめん" in sentence else "happy"
        assert emotion == expected, (sentence, by_sentence)
    done = next(event["data"] for event in events if event["event"] == "done")
    assert done["emotion"] == "happy"

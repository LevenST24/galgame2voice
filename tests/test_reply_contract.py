"""P2b reply contract, old-format stripping, history replay and old-mode compatibility."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.chat_pipelines import context_builder
from galgame2voice.services.chat_pipelines.acting_menu import build_acting_menu
from galgame2voice.services.chat_pipelines.context_builder import (
    BILINGUAL_OUTPUT_CONTRACT,
    build_chat_context,
)
from galgame2voice.services.chat_pipelines.line_scanner import EMOTIONS, FX, MOTIONS
from galgame2voice.services.chat_pipelines.reply_contract import (
    format_lines_turn,
    render_lines_contract,
    strip_old_format_block,
)
from galgame2voice.services.session_manager import SessionManager, SessionTurn

MENU = {
    "outfit": "uniform",
    "faces": ["微笑", "生气"],
    "face_ids": {"微笑": ["smile"], "生气": ["angry"]},
    "poses": [],
    "blush": True,
    "motions": list(MOTIONS),
    "fx": list(FX),
}

SYNTHETIC_TEMPLATE_SECTION = (
    "【输出契约与情绪控制】\n"
    "重要：你必须严格输出如下 JSON 格式，在最开头根据语境动态决定语音推理参数（示例参数），"
    "不要输出任何多余文字、不要加 markdown 代码块标记：\n"
    '{"tts": {"speed": 1.0, "temp": 0.85, "emotion": "gentle"}, '
    '"chinese": "显示给玩家的中文台词", "japanese": "对应的口语化日文台词"}'
)

# Fixed fixture captured from the pre-change code path (HEAD), not recomputed.
HEAD_OLD_ASSISTANT = (
    '{"tts": {"speed": 1.0, "temp": 1.0, "emotion": "shy"}, '
    '"chinese": "早上好呀", "japanese": "おはようございます", "emotion": "shy"}'
)
HEAD_OLD_CONTRACT_ADDITION = (
    "【输出格式】请始终只输出如下 JSON，不要包含 Markdown 标记或多余文字。"
    "在最开头根据语境动态决定语音推理参数（speed 语速: 0.5~1.5, temp 温度: 0.60~1.20, "
    "emotion 情绪: gentle|shy|happy|tsundere|cool|sad|angry）；japanese 是与 chinese 意思一致、"
    "适合配音朗读的口语化日语：\n"
    '{"tts": {"speed": 1.05, "temp": 0.95, "emotion": "gentle"}, '
    '"chinese": "给玩家看的中文内容", "japanese": "对应的口语化日语音频台词"}'
)


class TestRenderLinesContract:
    @pytest.mark.parametrize("faces", [["微笑", "生气"], []])
    def test_multiple_poses_are_optional_named_choices(self, faces):
        text = render_lines_contract({**MENU, "faces": faces, "poses": ["a", "b"],
                                      "pose_names": {"a": "站姿1", "b": "站姿2"}})
        example = json.loads(text.splitlines()[1])
        assert example["lines"][0]["act"]["pose"] == "a"
        assert "pose 可省略" in text
        assert "只能取：a（站姿1）、b（站姿2）" in text
        assert "省略时保持当前姿势" in text
        assert "不要输出 index、audio 或 outfit。" in text
        assert "outfit 或 pose" not in text

    @pytest.mark.parametrize("poses", [[], ["a"]])
    def test_fewer_than_two_poses_keep_exact_contract(self, poses):
        expected = (
            "【输出格式】请始终只输出如下 JSON，不要包含 Markdown 标记或多余文字：\n"
            '{"tts": {"speed": 1.05, "temp": 0.95, "emotion": "gentle"}, "lines": ['
            '{"zh": "给玩家看的中文台词", "ja": "意思相同的口语化日语台词", '
            '"emotion": "gentle", "act": {"face": "表情标签"}}]}\n'
            f"每行只写一句适合配音朗读的话，zh 与 ja 意思一致；emotion 必填，只能是 {'|'.join(EMOTIONS)}。\n"
            "act 可省略；不要输出 index、audio、outfit 或 pose。\n"
            "face 通常每行都给出，只能取：微笑、生气。\n"
            f"motion 很少使用，大多数行不写；允许值：{'、'.join(MOTIONS)}。\n"
            f"fx 很少使用，大多数行不写；允许值：{'、'.join(FX)}。\n"
            "blush 仅在需要时设为 true，省略或 false 表示不添加。"
        )
        assert render_lines_contract({**MENU, "poses": poses, "pose_names": {"a": "站姿1"}}) == expected

    def test_contract_lists_the_menu_choices(self):
        text = render_lines_contract(MENU)

        assert '"lines"' in text
        assert '"tts"' in text
        for token in ("zh", "ja", "emotion", "act", "微笑", "生气", "blush", "motion", "fx"):
            assert token in text
        for token in EMOTIONS + MOTIONS + FX:
            assert token in text
        assert "face 通常每行都给出" in text
        assert "不要输出 index、audio、outfit 或 pose" in text

    def test_blush_is_offered_only_when_available(self):
        assert "blush 仅在需要时设为 true" in render_lines_contract(MENU)
        assert "blush 仅在需要时设为 true" not in render_lines_contract({**MENU, "blush": False})

    def test_without_menu_no_face_is_offered(self):
        text = render_lines_contract(None)

        assert "face" not in text
        assert "blush" not in text
        assert "微笑" not in text
        for token in MOTIONS + FX:
            assert token in text

    def test_motion_and_fx_are_described_as_rare(self):
        text = render_lines_contract(MENU)
        assert "motion 很少使用" in text
        assert "fx 很少使用" in text


class TestStripOldFormatBlock:
    def test_removes_the_bilingual_contract(self):
        prompt = "你是测试角色。\n\n" + BILINGUAL_OUTPUT_CONTRACT
        assert strip_old_format_block(prompt) == "你是测试角色。"
        assert strip_old_format_block(BILINGUAL_OUTPUT_CONTRACT) == ""

    def test_removes_the_shared_template_section_but_keeps_persona_and_rules(self):
        prompt = (
            "你是合成测试角色，说话温柔。\n\n"
            + SYNTHETIC_TEMPLATE_SECTION
            + "\n\n要求：\n1. 只使用合成测试文本。"
        )

        stripped = strip_old_format_block(prompt)

        assert stripped.startswith("你是合成测试角色")
        assert "重要：你必须严格输出如下 JSON 格式" not in stripped
        assert "显示给玩家的中文台词" not in stripped
        assert "只使用合成测试文本" in stripped

    def test_removes_the_headerless_variant_with_different_numbers(self):
        section = SYNTHETIC_TEMPLATE_SECTION.split("\n", 1)[1]
        section = section.replace('"speed": 1.0', '"speed": 1.1')
        section = section.replace('"emotion": "gentle"', '"emotion": "happy"')
        prompt = "合成角色。\n\n" + section + "\n\n要求：\n1. 合成规则。"

        stripped = strip_old_format_block(prompt)

        assert stripped == "合成角色。\n\n要求：\n1. 合成规则。"

    def test_prompt_without_markers_is_returned_unchanged(self):
        prompt = "你是测试角色。\n没有格式段。"
        assert strip_old_format_block(prompt) == prompt

    def test_start_marker_without_end_marker_is_returned_unchanged(self):
        prompt = "角色。\n重要：你必须严格输出如下 JSON 格式，但没有结尾。"
        assert strip_old_format_block(prompt) == prompt


class TestFormatLinesTurn:
    def test_stored_lines_are_replayed_without_index_or_audio(self):
        lines = [
            {
                "index": 0,
                "zh": "早上好。",
                "ja": "おはよう。",
                "emotion": "happy",
                "act": {"face": "微笑", "motion": "nod"},
                "audio": {"status": "ready", "chunks": []},
            },
            {
                "index": 1,
                "zh": "再见。",
                "ja": "さようなら。",
                "emotion": "gentle",
                "act": {},
                "audio": {"status": "skipped", "chunks": []},
            },
        ]

        raw = format_lines_turn(
            lines,
            chinese="早上好。再见。",
            japanese="おはよう。さようなら。",
            emotion="happy",
            tts=None,
        )

        payload = json.loads(raw)
        assert payload == {
            "tts": {"speed": 1.0, "temp": 1.0, "emotion": "happy"},
            "lines": [
                {"zh": "早上好。", "ja": "おはよう。", "emotion": "happy", "act": {"face": "微笑", "motion": "nod"}},
                {"zh": "再见。", "ja": "さようなら。", "emotion": "gentle", "act": {}},
            ],
        }
        assert "index" not in raw
        assert "audio" not in raw

    def test_old_message_without_lines_is_built_from_text(self):
        raw = format_lines_turn(
            None,
            chinese="你好。再见。",
            japanese="こんにちは。さようなら。",
            emotion="gentle",
            tts={"speed": 1.1, "temp": 0.8},
        )

        payload = json.loads(raw)
        assert payload["tts"] == {"speed": 1.1, "temp": 0.8, "emotion": "gentle"}
        assert [line["zh"] for line in payload["lines"]] == ["你好。", "再见。"]
        assert [line["ja"] for line in payload["lines"]] == ["こんにちは。", "さようなら。"]
        assert all(line["act"] == {} for line in payload["lines"])

    def test_tts_emotion_falls_back_to_the_turn_emotion(self):
        raw = format_lines_turn(None, chinese="你好。", japanese="こんにちは。", emotion="shy", tts=None)
        assert json.loads(raw)["tts"]["emotion"] == "shy"


class TestOldModeIsUnchanged:
    def test_format_llm_messages_matches_the_head_fixture(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        history = [
            SessionTurn(
                role="assistant",
                content_chinese="早上好呀",
                content_japanese="おはようございます",
                emotion="shy",
            )
        ]

        expected = [
            {"role": "system", "content": "你是测试角色。"},
            {"role": "assistant", "content": HEAD_OLD_ASSISTANT},
        ]

        assert sm.format_llm_messages("测试角色", history, system_template="你是{character_name}。") == expected
        assert sm.format_llm_messages(
            "测试角色", history, system_template="你是{character_name}。", reply_format="old"
        ) == expected

    @pytest.mark.asyncio
    async def test_build_chat_context_matches_the_head_fixture(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        profile = SimpleNamespace(id=1, name="测试角色", system_prompt="你是测试角色。")
        session = SimpleNamespace(user_id="user-1")

        async with get_db(temp_db_path) as conn:
            messages = await build_chat_context(
                conn=conn,
                session_id="old-mode",
                user_prompt="你好",
                session_manager=sm,
                memory_service=None,
                active_profile=profile,
                session=session,
            )

        assert [message.role for message in messages] == ["system", "user"]
        assert messages[0].content == "你是测试角色。\n\n" + HEAD_OLD_CONTRACT_ADDITION
        assert messages[1].content == "你好"


class TestLinesModeWiring:
    def test_helper_builds_the_menu_from_the_character_portrait(self, monkeypatch):
        portrait = {
            "default_costume": "uniform",
            "costumes": [{"id": "uniform"}],
            "sprites": {"uniform": {"smile": {"file": "smile.png"}}},
            "faces": {"smile": {"label": "微笑", "blush": None}},
        }
        package = SimpleNamespace(folder=Path("synthetic-pack"))
        manager = SimpleNamespace(get_character=lambda name: package if name == "测试角色" else None)
        monkeypatch.setattr(context_builder, "get_character_manager", lambda: manager)
        monkeypatch.setattr(context_builder, "load_portrait_manifest", lambda folder: portrait)

        assert context_builder._build_acting_menu("测试角色") == build_acting_menu(portrait)
        assert context_builder._build_acting_menu("不存在的角色") is None

    @pytest.mark.asyncio
    async def test_build_chat_context_lines_mode_replaces_the_old_block(self, temp_db_path, monkeypatch):
        sm = SessionManager(temp_db_path)
        prompt = (
            "你是合成测试角色，说话温柔。\n\n"
            + SYNTHETIC_TEMPLATE_SECTION
            + "\n\n要求：\n1. 只使用合成测试文本。"
        )
        profile = SimpleNamespace(id=1, name="测试角色", system_prompt=prompt)
        session = SimpleNamespace(user_id="user-1")
        monkeypatch.setattr(context_builder, "_build_acting_menu", lambda name: MENU)

        async with get_db(temp_db_path) as conn:
            messages = await build_chat_context(
                conn=conn,
                session_id="lines-mode",
                user_prompt="你好",
                session_manager=sm,
                memory_service=None,
                active_profile=profile,
                session=session,
                reply_format="lines",
            )

        system = messages[0].content
        assert system.startswith("你是合成测试角色")
        assert "重要：你必须严格输出如下 JSON 格式" not in system
        assert "显示给玩家的中文台词" not in system
        assert "只使用合成测试文本" in system
        assert system.endswith(render_lines_contract(MENU))

    @pytest.mark.asyncio
    async def test_build_chat_context_lines_mode_without_a_menu(self, temp_db_path, monkeypatch):
        sm = SessionManager(temp_db_path)
        session = SimpleNamespace(user_id="user-1", voice_profile_id=None)
        monkeypatch.setattr(context_builder, "_build_acting_menu", lambda name: None)

        async with get_db(temp_db_path) as conn:
            messages = await build_chat_context(
                conn=conn,
                session_id="lines-mode-no-menu",
                user_prompt="你好",
                session_manager=sm,
                memory_service=None,
                system_prompt_override="你是测试角色。",
                session=session,
                reply_format="lines",
            )

        assert messages[0].content == "你是测试角色。\n\n" + render_lines_contract(None)

    def test_format_llm_messages_lines_mode_replays_stored_lines(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        history = [
            SessionTurn(
                role="assistant",
                content_chinese="早上好呀",
                content_japanese="おはようございます",
                emotion="happy",
                lines=[
                    {
                        "index": 0,
                        "zh": "早上好呀",
                        "ja": "おはようございます",
                        "emotion": "happy",
                        "act": {"face": "微笑"},
                        "audio": {"status": "ready", "chunks": []},
                    }
                ],
            )
        ]

        messages = sm.format_llm_messages(
            "测试角色", history, system_template="你是{character_name}。", reply_format="lines"
        )

        payload = json.loads(messages[1]["content"])
        assert payload == {
            "tts": {"speed": 1.0, "temp": 1.0, "emotion": "happy"},
            "lines": [{"zh": "早上好呀", "ja": "おはようございます", "emotion": "happy", "act": {"face": "微笑"}}],
        }

    def test_format_llm_messages_lines_mode_rebuilds_a_turn_without_lines(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        history = [
            SessionTurn(
                role="assistant",
                content_chinese="你好。再见。",
                content_japanese="こんにちは。さようなら。",
                emotion="gentle",
            )
        ]

        messages = sm.format_llm_messages(
            "测试角色", history, system_template="你是{character_name}。", reply_format="lines"
        )

        payload = json.loads(messages[1]["content"])
        assert len(payload["lines"]) == 2
        assert [line["zh"] for line in payload["lines"]] == ["你好。", "再见。"]

    @pytest.mark.asyncio
    async def test_build_chat_messages_lines_mode_reads_stored_rows(self, tmp_path):
        db_path = tmp_path / "lines-history.db"
        await init_db(db_path)
        sm = SessionManager(db_path)
        stored = [
            {
                "index": 0,
                "zh": "你好。",
                "ja": "こんにちは。",
                "emotion": "happy",
                "act": {"face": "微笑"},
                "audio": {"status": "ready", "chunks": []},
            }
        ]
        await sm.add_turn("lines-history", "user", "你好。", "")
        await sm.add_turn("lines-history", "assistant", "你好。", "こんにちは。", lines=stored)

        messages = await sm.build_chat_messages(
            "lines-history",
            "下一句",
            character_name="测试角色",
            custom_system_prompt="你是测试角色。",
            reply_format="lines",
        )

        assert [message.role for message in messages] == ["system", "user", "assistant", "user"]
        payload = json.loads(messages[2].content)
        assert payload["lines"] == [
            {"zh": "你好。", "ja": "こんにちは。", "emotion": "happy", "act": {"face": "微笑"}}
        ]

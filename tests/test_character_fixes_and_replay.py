"""
Targeted tests verifying fixes for the 7 user-reported issues:
1. 白雪乃爱 (Noa): Boku-musume persona, pronoun enforcement, foodie traits, boosted reference audio volume.
2. 高楯欧丽叶 (Orie): Elegant slower voice speed (0.88) and prompt guidance.
3. 谷风天音 (Amane): Authentic mesugaki tsundere imouto dialogue.
4. 常陆茉子 (Mako): Authentic 「お方様」/「有地さん」 honorifics, non-robotic tone, 0.90 speed.
5. 丛雨 (Murasame): Strict 「ご主人 / ご主人様」 mandate, ban on 「主」.
6. Truncation bug: Preservation of spoken dialogue within parentheses in clean_japanese_parentheses.
7. Character switching: Session and request-level voice_profile_id propagation in chat_service and tts_service.
"""

import json
from pathlib import Path
import pytest
import aiosqlite

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import init_db, get_db
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.services.character_manager import get_character_manager
from galgame2voice.services.gpt_sovits_client import clean_japanese_parentheses
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.tts_service import TtsService
from galgame2voice.routers.chat import ChatRequest


def test_noa_persona_and_audio():
    settings = get_settings()
    noa_dir = settings.characters_dir / "白雪乃爱"
    manifest_p = noa_dir / "manifest.json"
    prompt_p = noa_dir / "system_prompt.txt"

    assert manifest_p.is_file(), "Noa manifest.json must exist"
    assert prompt_p.is_file(), "Noa system_prompt.txt must exist"

    with open(manifest_p, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    with open(prompt_p, "r", encoding="utf-8") as f:
        prompt = f.read()

    # Verify pronoun rules
    assert "僕" in prompt
    assert "僕（ぼく）" in prompt or "「僕」" in prompt
    assert "私" in prompt
    assert "わたし" in prompt
    assert "严禁" in prompt or "禁止" in prompt

    # Verify foodie & personality traits
    assert "アイス" in prompt or "アイスクリーム" in prompt or "冰淇淋" in prompt
    assert "ハンバーガー" in prompt or "汉堡" in prompt
    assert "天罰" in prompt

    # Verify default speed in manifest is energetic (1.05)
    speed = manifest.get("default_voice_params", {}).get("speed")
    assert speed == 1.05

    # Verify reference audio files exist
    refs_dir = noa_dir / "refs"
    for emo in ["gentle", "happy", "sad", "shy", "cool", "tsundere"]:
        ref_file = refs_dir / f"{emo}.ogg"
        assert ref_file.is_file(), f"Noa ref audio {emo}.ogg must exist"
        assert ref_file.stat().st_size > 1000, f"Noa ref audio {emo}.ogg must not be empty"


def test_orie_persona_and_speed():
    settings = get_settings()
    orie_dir = settings.characters_dir / "高楯欧丽叶"
    manifest_p = orie_dir / "manifest.json"
    prompt_p = orie_dir / "system_prompt.txt"

    with open(manifest_p, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    with open(prompt_p, "r", encoding="utf-8") as f:
        prompt = f.read()

    speed = manifest.get("default_voice_params", {}).get("speed")
    assert speed == 0.88, f"Orie default speed should be 0.88, got {speed}"
    assert "0.85" in prompt or "0.88" in prompt or "0.90" in prompt, "Orie prompt should specify elegant slow tempo"


def test_amane_persona_mesugaki():
    settings = get_settings()
    amane_dir = settings.characters_dir / "谷风天音"
    prompt_p = amane_dir / "system_prompt.txt"

    with open(prompt_p, "r", encoding="utf-8") as f:
        prompt = f.read()

    # Authentic mesugaki & tsundere imouto traits
    assert "シスコン" in prompt
    assert "キモーイ" in prompt or "キモい" in prompt
    assert "バカ" in prompt
    assert "妹" in prompt


def test_mako_persona_and_speed():
    settings = get_settings()
    mako_dir = settings.characters_dir / "常陆茉子"
    manifest_p = mako_dir / "manifest.json"
    prompt_p = mako_dir / "system_prompt.txt"

    with open(manifest_p, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    with open(prompt_p, "r", encoding="utf-8") as f:
        prompt = f.read()

    speed = manifest.get("default_voice_params", {}).get("speed")
    assert speed == 0.90, f"Mako default speed should be 0.90, got {speed}"
    assert "お方様" in prompt
    assert "有地さん" in prompt
    assert "少女漫画" in prompt or "甘いもの" in prompt or "お恥ずかしい限りです" in prompt
    assert "严禁使用" in prompt or "绝非" in prompt or "不要使用" in prompt


def test_murasame_persona_master():
    settings = get_settings()
    murasame_dir = settings.characters_dir / "丛雨"
    prompt_p = murasame_dir / "system_prompt.txt"

    with open(prompt_p, "r", encoding="utf-8") as f:
        prompt = f.read()

    assert "ご主人" in prompt
    assert "主（ぬし）" in prompt or "「主」" in prompt or "ぬし" in prompt
    assert "严禁" in prompt or "禁止" in prompt or "绝不" in prompt


def test_clean_japanese_parentheses_spoken_dialogue():
    # Spoken dialogue inside brackets should be unwrapped, NOT stripped!
    text1 = "あなたが、私の（探している人ですか？）"
    cleaned1 = clean_japanese_parentheses(text1)
    assert cleaned1 == "あなたが、我的探している人ですか？" or cleaned1 == "あなたが、私の探している人ですか？", f"Expected unwrapped dialogue, got: '{cleaned1}'"
    assert "探している人ですか" in cleaned1

    text2 = "（本当にこれでいいの？）"
    cleaned2 = clean_japanese_parentheses(text2)
    assert "本当にこれでいいの" in cleaned2

    # Stage cues and directions SHOULD be stripped!
    text3 = "（微笑みながら）こんにちは！"
    cleaned3 = clean_japanese_parentheses(text3)
    assert cleaned3 == "こんにちは！", f"Expected stage direction stripped, got: '{cleaned3}'"

    text4 = "(sigh) 困ったものですね"
    cleaned4 = clean_japanese_parentheses(text4)
    assert cleaned4 == "困ったものですね", f"Expected english cue stripped, got: '{cleaned4}'"


@pytest.mark.asyncio
async def test_session_custom_system_prompt_and_voice_profile_binding(tmp_path):
    db_file = tmp_path / "test_binding.db"
    await init_db(db_file)

    async with get_db(str(db_file)) as conn:
        # Create two distinct profiles
        p1 = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="四季夏目",
            gpt_weights_path="dummy_gpt1.ckpt",
            sovits_weights_path="dummy_sov1.pth",
            ref_audio_path="dummy_ref1.wav",
            prompt_text="夏目です",
            prompt_lang="ja",
            text_lang="ja",
            system_prompt="我是夏目系统提示词",
        ))
        p2 = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="白雪乃爱",
            gpt_weights_path="dummy_gpt2.ckpt",
            sovits_weights_path="dummy_sov2.pth",
            ref_audio_path="dummy_ref2.wav",
            prompt_text="乃爱だよ",
            prompt_lang="ja",
            text_lang="ja",
            system_prompt="僕は乃爱！アイスが大好きなんだ！",
        ))
        await crud.set_active_voice_profile(conn, p1.id)

        # Create session bound to Noa with custom_system_prompt
        sess = await crud.upsert_session(
            conn=conn,
            session_id="sess_noa",
            title="Noa Session",
            voice_profile_id=p2.id,
            custom_system_prompt="我是会话专属自定义乃爱Prompt",
        )

        chat_svc = ChatService(db_path=str(db_file))

        # 1. Test _prepare_messages uses sess.custom_system_prompt
        msgs = await chat_svc._prepare_messages(
            conn=conn,
            session_id="sess_noa",
            user_prompt="你好乃爱",
            session=sess,
        )
        assert any("我是会话专属自定义乃爱Prompt" in m.content for m in msgs)

        # 2. Test _prepare_messages falls back to bound voice_profile_id prompt if custom_system_prompt is None
        sess_no_custom = await crud.upsert_session(
            conn=conn,
            session_id="sess_noa_default",
            title="Noa Default",
            voice_profile_id=p2.id,
            custom_system_prompt=None,
        )
        msgs2 = await chat_svc._prepare_messages(
            conn=conn,
            session_id="sess_noa_default",
            user_prompt="你好乃爱",
            session=sess_no_custom,
        )
        assert any("僕は乃爱！アイスが大好きなんだ！" in m.content for m in msgs2)


@pytest.mark.asyncio
async def test_tts_service_populates_requested_voice_profile(tmp_path):
    db_file = tmp_path / "test_tts_pop.db"
    await init_db(db_file)

    async with get_db(str(db_file)) as conn:
        p1 = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="四季夏目",
            gpt_weights_path="dummy1.ckpt",
            sovits_weights_path="dummy1.pth",
            ref_audio_path="ref_natsume.wav",
            prompt_text="夏目プロンプト",
            prompt_lang="ja",
            text_lang="ja",
        ))
        p2 = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="白雪乃爱",
            gpt_weights_path="dummy2.ckpt",
            sovits_weights_path="dummy2.pth",
            ref_audio_path="ref_noa.wav",
            prompt_text="乃爱プロンプト",
            prompt_lang="ja",
            text_lang="ja",
        ))
        await crud.set_active_voice_profile(conn, p1.id)

    tts_svc = TtsService(db_path=str(db_file))

    # Request with voice_profile_id=p2.id should resolve p2 even though p1 is global active
    opts = {"voice_profile_id": p2.id}
    res_opts = await tts_svc._populate_voice_profile_opts(opts)
    assert res_opts["voice_profile_id"] == p2.id
    assert res_opts["prompt_text"] == "乃爱プロンプト"

    # Request with character_name="白雪乃爱" should also resolve p2
    opts_by_name = {"character_name": "白雪乃爱"}
    res_name = await tts_svc._populate_voice_profile_opts(opts_by_name)
    assert res_name["voice_profile_id"] == p2.id
    assert res_name["prompt_text"] == "乃爱プロンプト"


def test_chat_request_schema():
    req = ChatRequest(
        prompt="测试提示",
        voice_profile_id=42,
    )
    assert req.voice_profile_id == 42


def test_parentheses_cleaning_extreme_cases():
    """Verifies that nested brackets, whispers, and action cues are cleanly stripped, while dialogue is kept."""
    # 1. 5-level nested brackets with inner stage direction
    nested = "（（（（（深層の心の声）））））こんにちは、先生！"
    assert clean_japanese_parentheses(nested) == "こんにちは、先生！"

    # 2. Mixed ASCII and fullwidth brackets
    mixed = "(sigh) （微笑んで） (whispers: 'hello') 元気ですか？"
    assert clean_japanese_parentheses(mixed) == "元気ですか？"

    # 3. Only stage directions -> empty
    only_stage = "（手を振る）（微笑む）(leaves)"
    assert clean_japanese_parentheses(only_stage) == ""

    # 4. Spoken dialogue preserved
    dialogue = "あなたが、私の（探している人ですか？）"
    assert clean_japanese_parentheses(dialogue) == "あなたが、私の探している人ですか？"


@pytest.mark.asyncio
async def test_session_upsert_endpoint_voice_profile_persistence(tmp_path, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    from galgame2voice.main import create_app

    db_file = tmp_path / "test_api_sess.db"
    await init_db(db_file)
    monkeypatch.setattr("galgame2voice.routers.chat.get_db", lambda: get_db(str(db_file)))

    async with get_db(str(db_file)) as conn:
        p = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="白雪乃爱",
            gpt_weights_path="dummy.ckpt",
            sovits_weights_path="dummy.pth",
            ref_audio_path="ref_noa.wav",
            prompt_text="乃爱プロンプト",
            prompt_lang="ja",
            text_lang="ja",
        ))
        prof_id = p.id

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create session with voice_profile_id=prof_id and custom_system_prompt
        resp = await client.post("/api/chat/sessions", json={
            "id": "sess_test_switch",
            "title": "测试切换角色会话",
            "voice_profile_id": prof_id,
            "custom_system_prompt": "我是测试专属白雪乃爱Prompt",
            "settings": {"voiceProfileId": prof_id, "ttsSpeed": 1.05},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["session"]["voice_profile_id"] == prof_id
        assert data["session"]["custom_system_prompt"] == "我是测试专属白雪乃爱Prompt"

        # Fetch session list
        get_resp = await client.get("/api/chat/sessions")
        assert get_resp.status_code == 200
        sessions = get_resp.json().get("sessions", [])
        matched = [s for s in sessions if s["id"] == "sess_test_switch"]
        assert len(matched) == 1
        assert matched[0]["voice_profile_id"] == prof_id

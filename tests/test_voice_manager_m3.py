"""
Comprehensive M3 Tests for GPT-SoVITS Client, Voice Manager, Stage Directions Cleaner,
Transactional Weight Switching with Auto-Rollback, and FastAPI Voice API Router.
"""

import asyncio
import io
import json
import pytest
from httpx import AsyncClient, ASGITransport
import aiosqlite

from galgame2voice.services.gpt_sovits_client import (
    GptSovitsClient,
    clean_japanese_parentheses,
    resolve_tts_options,
    SLICING_METHODS,
    TTS_PRESETS,
)
from galgame2voice.services.voice_manager import (
    VoiceManager,
    get_voice_manager,
    set_voice_manager,
)
from galgame2voice.services.tts_service import TtsService
from galgame2voice.database.models import (
    VoiceProfileCreate,
    VoiceProfileUpdate,
    VoiceProfileResponse,
)
from galgame2voice.main import create_app
from tests.conftest import MockGptSovitsServer


# ============================================================================
# 1. Tests for Japanese Parentheses Cleaner
# ============================================================================

class TestJapaneseParenthesesCleaner:
    """Test suite for stage directions and cues cleaning."""

    def test_basic_fullwidth_brackets(self):
        text = "（微笑みながら）先生、おはようございます！（手を振る）"
        assert clean_japanese_parentheses(text) == "先生、おはようございます！"

    def test_basic_halfwidth_brackets(self):
        text = "(giggles) こんにちは、指揮官！ (nods)"
        assert clean_japanese_parentheses(text) == "こんにちは、指揮官！"

    def test_nested_brackets(self):
        text = "（（深呼吸して）落ち着いて）大丈夫ですよ、先輩。"
        assert clean_japanese_parentheses(text) == "大丈夫ですよ、先輩。"

    def test_triple_nested_brackets(self):
        text = "（（（ため息）））やっと終わりました。"
        assert clean_japanese_parentheses(text) == "やっと終わりました。"

    def test_unmatched_brackets_handled_gracefully(self):
        text = "（未完了の括弧 こんにちは"
        assert clean_japanese_parentheses(text) == "未完了の括弧 こんにちは"

    def test_closing_bracket_only(self):
        text = "こんにちは）マスター"
        assert clean_japanese_parentheses(text) == "こんにちはマスター"

    def test_empty_and_whitespace(self):
        assert clean_japanese_parentheses("") == ""
        assert clean_japanese_parentheses("   ") == ""
        assert clean_japanese_parentheses(None) == ""

    def test_stage_directions_only_becomes_empty(self):
        assert clean_japanese_parentheses("（笑）（ため息）（静寂）") == ""

    def test_pure_text_unchanged(self):
        pure = "先生、準備は完了しました！いつでも出発できます。"
        assert clean_japanese_parentheses(pure) == pure


# ============================================================================
# 2. Tests for Presets and Option Resolution
# ============================================================================

class TestPresetResolution:
    """Test suite for TTS preset options and slicing configurations."""

    def test_preset_definitions_exist(self):
        assert "high_quality" in TTS_PRESETS
        assert "balanced" in TTS_PRESETS
        assert "low_latency" in TTS_PRESETS

    def test_slicing_methods_coverage(self):
        for cut in ["cut0", "cut1", "cut2", "cut3", "cut4", "cut5"]:
            assert cut in SLICING_METHODS

    def test_resolve_preset_high_quality(self):
        opts = resolve_tts_options({"preset": "high_quality"})
        assert opts["top_k"] == 20
        assert opts["temperature"] == 0.8
        assert opts["text_split_method"] == "cut5"

    def test_resolve_preset_low_latency(self):
        opts = resolve_tts_options({"preset": "low_latency"})
        assert opts["top_k"] == 5
        assert opts["temperature"] == 0.5
        assert opts["speed"] == 1.2
        assert opts["text_split_method"] == "cut5"

    def test_custom_options_override_preset(self):
        opts = resolve_tts_options({
            "preset": "balanced",
            "speed": 1.5,
            "top_k": 30,
            "text_split_method": "cut3",
        })
        assert opts["speed"] == 1.5
        assert opts["top_k"] == 30
        assert opts["text_split_method"] == "cut3"


# ============================================================================
# 3. Tests for GptSovitsClient (Endpoints, Mutex, Rollback, Streaming)
# ============================================================================

class TestGptSovitsClientM3:
    """Test suite for GptSovitsClient integration."""

    @pytest.mark.asyncio
    async def test_client_endpoints_and_health(self, mock_gpt_sovits):
        client = GptSovitsClient(server=mock_gpt_sovits)
        health = await client.check_health()
        assert health["connected"] is True
        assert health["status"] == "running"

        control_res = await client.control("restart")
        assert control_res["status"] == "running"

    @pytest.mark.asyncio
    async def test_set_individual_weights(self, mock_gpt_sovits):
        client = GptSovitsClient(server=mock_gpt_sovits)
        
        ok_gpt = await client.set_gpt_weights("weights/custom.ckpt")
        assert ok_gpt is True
        assert mock_gpt_sovits.current_gpt_weights == "weights/custom.ckpt"

        ok_sovits = await client.set_sovits_weights("weights/custom.pth")
        assert ok_sovits is True
        assert mock_gpt_sovits.current_sovits_weights == "weights/custom.pth"

        ok_ref = await client.set_refer_audio("ref/custom.wav", "カスタムテキスト", "ja")
        assert ok_ref is True
        assert mock_gpt_sovits.current_refer_audio == "ref/custom.wav"

    @pytest.mark.asyncio
    async def test_3step_switch_success(self, mock_gpt_sovits, sample_voice_profile):
        client = GptSovitsClient(server=mock_gpt_sovits)
        success = await client.switch_voice_profile(sample_voice_profile)
        assert success is True
        assert mock_gpt_sovits.current_gpt_weights == sample_voice_profile["gpt_weights_path"]
        assert mock_gpt_sovits.current_sovits_weights == sample_voice_profile["sovits_weights_path"]
        assert mock_gpt_sovits.current_refer_audio == sample_voice_profile["refer_audio_path"]

    @pytest.mark.asyncio
    async def test_rollback_on_step2_sovits_failure(self, mock_gpt_sovits):
        client = GptSovitsClient(server=mock_gpt_sovits)
        profile_a = {
            "name": "ProfileA",
            "gpt_weights_path": "weights/a.ckpt",
            "sovits_weights_path": "weights/a.pth",
            "refer_audio_path": "ref/a.wav",
            "refer_text": "テキストA"
        }
        await client.switch_voice_profile(profile_a)
        assert mock_gpt_sovits.current_gpt_weights == "weights/a.ckpt"

        profile_b = {
            "name": "ProfileB",
            "gpt_weights_path": "weights/b.ckpt",
            "sovits_weights_path": "weights/b.pth",
            "refer_audio_path": "ref/b.wav",
            "refer_text": "テキストB"
        }
        mock_gpt_sovits.fail_on_step("set_sovits_weights")
        success = await client.switch_voice_profile(profile_b)
        assert success is False
        # Rollback check: GPT weights restored to profile A
        assert mock_gpt_sovits.current_gpt_weights == "weights/a.ckpt"

    @pytest.mark.asyncio
    async def test_rollback_on_step3_refer_audio_failure(self, mock_gpt_sovits):
        client = GptSovitsClient(server=mock_gpt_sovits)
        profile_a = {
            "name": "ProfileA",
            "gpt_weights_path": "weights/a.ckpt",
            "sovits_weights_path": "weights/a.pth",
            "refer_audio_path": "ref/a.wav",
            "refer_text": "テキストA"
        }
        await client.switch_voice_profile(profile_a)

        profile_b = {
            "name": "ProfileB",
            "gpt_weights_path": "weights/b.ckpt",
            "sovits_weights_path": "weights/b.pth",
            "refer_audio_path": "ref/b.wav",
            "refer_text": "テキストB"
        }
        mock_gpt_sovits.fail_on_step("set_refer_audio")
        success = await client.switch_voice_profile(profile_b)
        assert success is False
        # Rollback check: Both GPT and SoVITS weights restored to profile A
        assert mock_gpt_sovits.current_gpt_weights == "weights/a.ckpt"
        assert mock_gpt_sovits.current_sovits_weights == "weights/a.pth"

    @pytest.mark.asyncio
    async def test_synthesize_and_streaming(self, mock_gpt_sovits, sample_voice_profile):
        client = GptSovitsClient(server=mock_gpt_sovits)
        await client.switch_voice_profile(sample_voice_profile)

        # Full synthesize
        audio_bytes = await client.synthesize("（笑顔で）こんにちは、先生！")
        assert audio_bytes.startswith(b"RIFF")
        assert len(audio_bytes) > 44

        # Stream TTS
        chunks = []
        async for chunk in client.stream_tts("（優しく）今日もお疲れ様でした。", chunk_size=512):
            chunks.append(chunk)

        combined = b"".join(chunks)
        assert combined.startswith(b"RIFF")
        assert len(chunks) > 1

    @pytest.mark.asyncio
    async def test_inference_mutex_serialization(self, mock_gpt_sovits):
        client = GptSovitsClient(server=mock_gpt_sovits)
        mock_gpt_sovits.simulate_latency_s = 0.05

        p1 = {"name": "P1", "gpt_weights_path": "w1.ckpt", "sovits_weights_path": "w1.pth", "refer_audio_path": "r1.wav", "refer_text": "t1"}
        p2 = {"name": "P2", "gpt_weights_path": "w2.ckpt", "sovits_weights_path": "w2.pth", "refer_audio_path": "r2.wav", "refer_text": "t2"}

        res1, res2 = await asyncio.gather(
            client.switch_voice_profile(p1),
            client.switch_voice_profile(p2),
        )
        assert res1 is True
        assert res2 is True
        assert mock_gpt_sovits.max_concurrent_seen == 1


# ============================================================================
# 4. Tests for VoiceManager & SQLite Persistence
# ============================================================================

class TestVoiceManagerPersistenceM3:
    """Test suite for VoiceManager coordinating SQLite and GPT-SoVITS."""

    @pytest.mark.asyncio
    async def test_switch_profile_by_db_id(self, temp_db_path, mock_gpt_sovits, sample_voice_profile):
        manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)

        # Insert voice profile into database
        async with aiosqlite.connect(temp_db_path) as db:
            cursor = await db.execute("""
                INSERT INTO voice_profiles (name, gpt_weights_path, sovits_weights_path, refer_audio_path, refer_text, refer_language, is_default)
                VALUES (?, ?, ?, ?, ?, ?, 1);
            """, (
                sample_voice_profile["name"],
                sample_voice_profile["gpt_weights_path"],
                sample_voice_profile["sovits_weights_path"],
                sample_voice_profile["refer_audio_path"],
                sample_voice_profile["refer_text"],
                sample_voice_profile["refer_language"],
            ))
            profile_id = cursor.lastrowid
            await db.commit()

        # Switch by profile ID
        success = await manager.switch_profile(profile_id, persist=True)
        assert success is True
        assert mock_gpt_sovits.current_gpt_weights == sample_voice_profile["gpt_weights_path"]

        # Verify active profile in DB settings
        active_prof = await manager.get_active_profile()
        assert active_prof is not None
        assert active_prof.id == profile_id

    @pytest.mark.asyncio
    async def test_switch_profile_by_name(self, temp_db_path, mock_gpt_sovits, sample_voice_profile):
        manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)

        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("""
                INSERT INTO voice_profiles (name, gpt_weights_path, sovits_weights_path, refer_audio_path, refer_text, refer_language, is_default)
                VALUES (?, ?, ?, ?, ?, ?, 0);
            """, (
                sample_voice_profile["name"],
                sample_voice_profile["gpt_weights_path"],
                sample_voice_profile["sovits_weights_path"],
                sample_voice_profile["refer_audio_path"],
                sample_voice_profile["refer_text"],
                sample_voice_profile["refer_language"],
            ))
            await db.commit()

        success = await manager.switch_profile("Arona", persist=False)
        assert success is True
        assert mock_gpt_sovits.current_gpt_weights == sample_voice_profile["gpt_weights_path"]

    @pytest.mark.asyncio
    async def test_tts_service_file_writing(self, mock_gpt_sovits, tmp_path, sample_voice_profile):
        client = GptSovitsClient(server=mock_gpt_sovits)
        await client.switch_voice_profile(sample_voice_profile)

        service = TtsService(client=client, audio_dir=tmp_path)
        url_path, local_path, count = await service.synthesize_to_file("テスト音声です。")

        assert url_path.startswith("/audio/")
        assert local_path.exists()
        assert count > 44
        assert local_path.read_bytes().startswith(b"RIFF")


# ============================================================================
# 5. Tests for FastAPI Voice API Router Endpoints
# ============================================================================

class TestVoiceRouterEndpointsM3:
    """Test suite for /api/voice/* endpoints on the production FastAPI application."""

    @pytest.mark.asyncio
    async def test_voice_profiles_crud_and_switch_api(self, temp_db_path, mock_gpt_sovits, sample_voice_profile):
        # Configure VoiceManager singleton with mock server
        manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
        set_voice_manager(manager)

        app = create_app()
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Create Voice Profile
            post_resp = await client.post("/api/voice/profiles", json=sample_voice_profile)
            assert post_resp.status_code == 201
            data = post_resp.json()
            profile_id = data["id"]
            assert data["name"] == "Arona"

            # 2. List Profiles
            list_resp = await client.get("/api/voice/profiles")
            assert list_resp.status_code == 200
            profiles = list_resp.json()["profiles"]
            assert len(profiles) >= 1

            # 3. Get Single Profile
            get_resp = await client.get(f"/api/voice/profiles/{profile_id}")
            assert get_resp.status_code == 200
            assert get_resp.json()["profile"]["name"] == "Arona"

            # 4. Switch Voice Profile
            switch_resp = await client.post("/api/voice/switch", json={"profile_id": profile_id})
            assert switch_resp.status_code == 200
            assert switch_resp.json()["status"] == "switched"
            assert mock_gpt_sovits.current_gpt_weights == sample_voice_profile["gpt_weights_path"]

            # 5. Synthesize Audio via API
            synth_resp = await client.post("/api/voice/synthesize", json={
                "text": "（微笑みながら）先生、おはようございます！",
                "preset": "high_quality"
            })
            assert synth_resp.status_code == 200
            assert synth_resp.headers["content-type"] == "audio/wav"
            assert synth_resp.content.startswith(b"RIFF")

            # 6. Presets API
            preset_resp = await client.get("/api/voice/presets")
            assert preset_resp.status_code == 200
            assert "presets" in preset_resp.json()
            assert "slicing_methods" in preset_resp.json()

            # 7. Delete Profile
            del_resp = await client.delete(f"/api/voice/profiles/{profile_id}")
            assert del_resp.status_code == 200
            assert del_resp.json()["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_voice_router_error_cases(self, temp_db_path, mock_gpt_sovits):
        manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
        set_voice_manager(manager)

        app = create_app()
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Empty text after cleaning
            resp = await client.post("/api/voice/synthesize", json={"text": "（笑）（ため息）"})
            assert resp.status_code == 422

            # Switch nonexistent profile
            resp_switch = await client.post("/api/voice/switch", json={"profile_id": 99999})
            assert resp_switch.status_code == 404

            # Switch missing id and name
            resp_missing = await client.post("/api/voice/switch", json={})
            assert resp_missing.status_code == 400

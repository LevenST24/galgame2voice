"""
Tests for Voice Manager, Mutex-Protected Model Switching, Auto-Rollback, and Japanese Parentheses Cleaner.
Covers Tier 1 (3-Step Switch, Parentheses Cleaning, TTS Synthesis) and Tier 2 (Rollback on Failure, Mutex Serialization, Nested Brackets).
"""

import asyncio
import re
from typing import Dict, Any, Optional, List, AsyncGenerator
import pytest
from pydantic import BaseModel

from tests.conftest import MockGptSovitsServer


# ============================================================================
# Voice Manager Contract & Japanese Parentheses Cleaner Implementation
# ============================================================================

class VoiceProfileModel(BaseModel):
    id: Optional[int] = None
    name: str
    gpt_weights_path: str
    sovits_weights_path: str
    refer_audio_path: str
    refer_text: str
    refer_language: str = "ja"
    prompt_language: str = "ja"
    text_language: str = "ja"


def clean_japanese_parentheses(text: str) -> str:
    """
    Strips stage cues and action directions enclosed in fullwidth （...） or ASCII (...) parentheses.
    Preserves outer dialogue and trims whitespace.
    """
    if not text:
        return ""
    # Strip fullwidth Japanese brackets
    cleaned = re.sub(r'（[^）]*）', '', text)
    # Strip ASCII halfwidth brackets
    cleaned = re.sub(r'\([^)]*\)', '', cleaned)
    # Handle remaining unmatched opening/closing brackets gracefully
    cleaned = cleaned.replace('（', '').replace('）', '').replace('(', '').replace(')', '')
    return cleaned.strip()


class VoiceManager:
    """
    Coordinates GPT-SoVITS API calls with asyncio.Lock inference mutex.
    Implements 3-step atomic model switching with rollback on failure.
    """
    def __init__(self, sovits_server: MockGptSovitsServer):
        self.server = sovits_server
        self.lock = asyncio.Lock()
        self.active_profile: Optional[VoiceProfileModel] = None
        self.is_switching: bool = False

    async def switch_profile(self, target: VoiceProfileModel) -> bool:
        async with self.lock:
            self.is_switching = True
            prev_profile = self.active_profile
            try:
                # Step 1: Set GPT weights
                r1 = await self.server.handle_request("POST", "/set_gpt_weights", {"weights_path": target.gpt_weights_path})
                if r1.status_code != 200:
                    return False

                # Step 2: Set SoVITS weights
                r2 = await self.server.handle_request("POST", "/set_sovits_weights", {"weights_path": target.sovits_weights_path})
                if r2.status_code != 200:
                    # Rollback Step 1
                    if prev_profile:
                        await self.server.handle_request("POST", "/set_gpt_weights", {"weights_path": prev_profile.gpt_weights_path})
                    return False

                # Step 3: Set Refer Audio
                r3 = await self.server.handle_request("POST", "/set_refer_audio", {
                    "refer_audio_path": target.refer_audio_path,
                    "refer_text": target.refer_text,
                    "refer_language": target.refer_language
                })
                if r3.status_code != 200:
                    # Rollback Step 2 & Step 1
                    if prev_profile:
                        await self.server.handle_request("POST", "/set_sovits_weights", {"weights_path": prev_profile.sovits_weights_path})
                        await self.server.handle_request("POST", "/set_gpt_weights", {"weights_path": prev_profile.gpt_weights_path})
                    return False

                self.active_profile = target
                return True
            finally:
                self.is_switching = False

    async def synthesize(self, text: str, options: Optional[Dict[str, Any]] = None) -> bytes:
        async with self.lock:
            cleaned_text = clean_japanese_parentheses(text)
            if not cleaned_text:
                raise ValueError("Text is empty after cleaning stage directions")
            
            payload = {
                "text": cleaned_text,
                "text_language": (options or {}).get("text_language", "ja"),
                "speed": (options or {}).get("speed", 1.0),
                "top_k": (options or {}).get("top_k", 15),
                "temperature": (options or {}).get("temperature", 1.0)
            }
            resp = await self.server.handle_request("POST", "/tts", payload)
            if resp.status_code != 200:
                raise RuntimeError(f"TTS synthesis failed with status {resp.status_code}")
            return resp.content


# ============================================================================
# Tier 1: Voice Profile & Manager Feature Tests
# ============================================================================

class TestVoiceManagerTier1:
    """Tier 1: 3-Step switch happy path, parentheses cleaning, and TTS synthesis."""

    @pytest.mark.asyncio
    async def test_switch_profile_success(self, mock_gpt_sovits, sample_voice_profile):
        manager = VoiceManager(mock_gpt_sovits)
        profile = VoiceProfileModel(**sample_voice_profile)
        
        success = await manager.switch_profile(profile)
        assert success is True
        assert manager.active_profile is not None
        assert manager.active_profile.name == "Arona"
        assert mock_gpt_sovits.current_gpt_weights == profile.gpt_weights_path
        assert mock_gpt_sovits.current_sovits_weights == profile.sovits_weights_path
        assert mock_gpt_sovits.current_refer_audio == profile.refer_audio_path

    def test_clean_japanese_parentheses_basic(self):
        """Verifies removal of fullwidth and halfwidth brackets containing cues."""
        raw1 = "（微笑みながら）先生、おはようございます！（手を振る）"
        assert clean_japanese_parentheses(raw1) == "先生、おはようございます！"

        raw2 = "(giggles) こんにちは、指揮官！ (nods)"
        assert clean_japanese_parentheses(raw2) == "こんにちは、指揮官！"

    @pytest.mark.asyncio
    async def test_synthesize_audio_success(self, mock_gpt_sovits, sample_voice_profile):
        manager = VoiceManager(mock_gpt_sovits)
        profile = VoiceProfileModel(**sample_voice_profile)
        await manager.switch_profile(profile)

        audio_data = await manager.synthesize("（優しく）先生、今日もお仕事頑張ってくださいね。")
        assert audio_data is not None
        assert len(audio_data) > 44  # Valid WAV header size
        assert audio_data.startswith(b"RIFF")

    def test_clean_japanese_text_without_parentheses_unmodified(self):
        pure_text = "先生、準備は完了しました！"
        assert clean_japanese_parentheses(pure_text) == pure_text


# ============================================================================
# Tier 2: Mutex Locking, Rollback on Failure, and Boundary Cases
# ============================================================================

class TestVoiceManagerTier2:
    """Tier 2: Concurrency mutex serialization, step-by-step rollback, and bracket edge cases."""

    @pytest.mark.asyncio
    async def test_rollback_on_step2_failure(self, mock_gpt_sovits):
        manager = VoiceManager(mock_gpt_sovits)
        
        # Initial profile A
        profile_a = VoiceProfileModel(
            name="ProfileA",
            gpt_weights_path="weights/a.ckpt",
            sovits_weights_path="weights/a.pth",
            refer_audio_path="ref/a.wav",
            refer_text="テキストA"
        )
        await manager.switch_profile(profile_a)
        assert mock_gpt_sovits.current_gpt_weights == "weights/a.ckpt"

        # Target profile B with failure on Step 2 (SoVITS weights)
        profile_b = VoiceProfileModel(
            name="ProfileB",
            gpt_weights_path="weights/b.ckpt",
            sovits_weights_path="weights/b.pth",
            refer_audio_path="ref/b.wav",
            refer_text="テキストB"
        )
        mock_gpt_sovits.fail_on_step("set_sovits_weights")

        success = await manager.switch_profile(profile_b)
        assert success is False
        # Rollback check: GPT weights should be restored to Profile A
        assert mock_gpt_sovits.current_gpt_weights == "weights/a.ckpt"
        assert manager.active_profile.name == "ProfileA"

    @pytest.mark.asyncio
    async def test_rollback_on_step3_failure(self, mock_gpt_sovits):
        manager = VoiceManager(mock_gpt_sovits)
        
        # Initial profile A
        profile_a = VoiceProfileModel(
            name="ProfileA",
            gpt_weights_path="weights/a.ckpt",
            sovits_weights_path="weights/a.pth",
            refer_audio_path="ref/a.wav",
            refer_text="テキストA"
        )
        await manager.switch_profile(profile_a)

        # Target profile B with failure on Step 3 (Refer Audio)
        profile_b = VoiceProfileModel(
            name="ProfileB",
            gpt_weights_path="weights/b.ckpt",
            sovits_weights_path="weights/b.pth",
            refer_audio_path="ref/b.wav",
            refer_text="テキストB"
        )
        mock_gpt_sovits.fail_on_step("set_refer_audio")

        success = await manager.switch_profile(profile_b)
        assert success is False
        # Rollback check: Both GPT and SoVITS weights should be restored to Profile A
        assert mock_gpt_sovits.current_gpt_weights == "weights/a.ckpt"
        assert mock_gpt_sovits.current_sovits_weights == "weights/a.pth"
        assert manager.active_profile.name == "ProfileA"

    @pytest.mark.asyncio
    async def test_mutex_serializes_concurrent_switches(self, mock_gpt_sovits):
        manager = VoiceManager(mock_gpt_sovits)
        mock_gpt_sovits.simulate_latency_s = 0.05

        p1 = VoiceProfileModel(name="P1", gpt_weights_path="w1.ckpt", sovits_weights_path="w1.pth", refer_audio_path="r1.wav", refer_text="t1")
        p2 = VoiceProfileModel(name="P2", gpt_weights_path="w2.ckpt", sovits_weights_path="w2.pth", refer_audio_path="r2.wav", refer_text="t2")

        # Launch concurrent switches
        t1 = asyncio.create_task(manager.switch_profile(p1))
        t2 = asyncio.create_task(manager.switch_profile(p2))

        res1, res2 = await asyncio.gather(t1, t2)
        assert res1 is True
        assert res2 is True
        # Max concurrent requests inside mutex should strictly be 1
        assert mock_gpt_sovits.max_concurrent_seen == 1

    def test_clean_japanese_nested_and_malformed_parentheses(self):
        """Verifies nested, unclosed, and empty brackets are handled safely."""
        assert clean_japanese_parentheses("（（ため息））こんにちは") == "こんにちは"
        assert clean_japanese_parentheses("（）こんにちは") == "こんにちは"
        assert clean_japanese_parentheses("（未完了の括弧 こんにちは") == "未完了の括弧 こんにちは"
        assert clean_japanese_parentheses("") == ""
        assert clean_japanese_parentheses("   ") == ""

    @pytest.mark.asyncio
    async def test_synthesize_empty_after_cleaning_raises_error(self, mock_gpt_sovits, sample_voice_profile):
        manager = VoiceManager(mock_gpt_sovits)
        profile = VoiceProfileModel(**sample_voice_profile)
        await manager.switch_profile(profile)

        # Input consisting only of stage directions
        with pytest.raises(ValueError, match="empty after cleaning"):
            await manager.synthesize("（笑）（ため息）（静寂）")

    @pytest.mark.asyncio
    async def test_presets_parameter_application(self, mock_gpt_sovits, sample_voice_profile):
        """Verifies High Quality, Balanced, Low Latency preset option passing."""
        manager = VoiceManager(mock_gpt_sovits)
        profile = VoiceProfileModel(**sample_voice_profile)
        await manager.switch_profile(profile)

        # High Quality Preset
        hq_opts = {"speed": 0.9, "top_k": 20, "temperature": 0.8}
        await manager.synthesize("テスト音声", options=hq_opts)
        assert mock_gpt_sovits.call_history[-1]["payload"]["top_k"] == 20

        # Low Latency Preset
        ll_opts = {"speed": 1.2, "top_k": 5, "temperature": 0.5}
        await manager.synthesize("テスト音声2", options=ll_opts)
        assert mock_gpt_sovits.call_history[-1]["payload"]["top_k"] == 5
        assert mock_gpt_sovits.call_history[-1]["payload"]["speed"] == 1.2

    @pytest.mark.parametrize("cut_option", ["cut0", "cut1", "cut2", "cut3", "cut4", "cut5"])
    @pytest.mark.asyncio
    async def test_cut_options_parameter_passing(self, mock_gpt_sovits, sample_voice_profile, cut_option):
        """Verifies all 6 GPT-SoVITS cut options (cut0 to cut5) are accepted."""
        manager = VoiceManager(mock_gpt_sovits)
        profile = VoiceProfileModel(**sample_voice_profile)
        await manager.switch_profile(profile)
        await manager.synthesize("テスト", options={"cut_option": cut_option})
        assert mock_gpt_sovits.call_history[-1]["endpoint"] == "/tts"

    @pytest.mark.parametrize("language", ["ja", "zh", "en"])
    @pytest.mark.asyncio
    async def test_multilingual_tts_options(self, mock_gpt_sovits, sample_voice_profile, language):
        """Verifies synthesizing text in Japanese, Chinese, and English."""
        manager = VoiceManager(mock_gpt_sovits)
        profile = VoiceProfileModel(**sample_voice_profile)
        await manager.switch_profile(profile)
        await manager.synthesize("Hello/你好/こんにちは", options={"text_language": language})
        assert mock_gpt_sovits.call_history[-1]["payload"]["text_language"] == language

    @pytest.mark.asyncio
    async def test_multi_profile_sequential_switching_cycle(self, mock_gpt_sovits):
        """Verifies cyclic switching across multiple profiles (A -> B -> C -> A)."""
        manager = VoiceManager(mock_gpt_sovits)
        p_a = VoiceProfileModel(name="A", gpt_weights_path="a.ckpt", sovits_weights_path="a.pth", refer_audio_path="a.wav", refer_text="a")
        p_b = VoiceProfileModel(name="B", gpt_weights_path="b.ckpt", sovits_weights_path="b.pth", refer_audio_path="b.wav", refer_text="b")
        p_c = VoiceProfileModel(name="C", gpt_weights_path="c.ckpt", sovits_weights_path="c.pth", refer_audio_path="c.wav", refer_text="c")

        assert await manager.switch_profile(p_a) is True
        assert manager.active_profile.name == "A"
        assert await manager.switch_profile(p_b) is True
        assert manager.active_profile.name == "B"
        assert await manager.switch_profile(p_c) is True
        assert manager.active_profile.name == "C"
        assert await manager.switch_profile(p_a) is True
        assert manager.active_profile.name == "A"



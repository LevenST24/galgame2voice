"""
GPT-SoVITS API Client and TTS Utilities for galgame2voice.
Wraps GPT-SoVITS HTTP endpoints with asyncio.Lock inference mutex,
3-step atomic model switching with rollback, stage directions cleaner,
presets, slicing methods, and streaming audio generator.
"""

import asyncio
import logging
import re
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger("galgame2voice.services.gpt_sovits_client")


# ============================================================================
# Japanese Parentheses Cleaner (Stage Direction Stripper)
# ============================================================================

def clean_japanese_parentheses(text: str, max_passes: int = 5) -> str:
    """
    Strips stage cues and action directions enclosed in fullwidth （...） or ASCII (...) parentheses.
    Applies multi-pass regex sanitization (up to max_passes) to handle nested brackets like （（ため息））.
    Cleans remaining unmatched brackets and trims whitespace.

    Examples:
        "（微笑みながら）先生、おはようございます！（手を振る）" -> "先生、おはようございます！"
        "(giggles) こんにちは、指揮官！ (nods)" -> "こんにちは、指揮官！"
        "（（ため息））こんにちは" -> "こんにちは"
        "（未完了の括弧 こんにちは" -> "未完了の括弧 こんにちは"
    """
    if not text:
        return ""

    cleaned = text
    # Multi-pass regex for nested and adjacent brackets
    for _ in range(max_passes):
        prev = cleaned
        # Strip fullwidth Japanese brackets
        cleaned = re.sub(r'（[^（）]*）', '', cleaned)
        # Strip ASCII halfwidth brackets
        cleaned = re.sub(r'\([^()]*\)', '', cleaned)
        if cleaned == prev:
            break

    # Handle remaining unmatched opening/closing brackets gracefully
    cleaned = cleaned.replace('（', '').replace('）', '').replace('(', '').replace(')', '')
    return cleaned.strip()


# ============================================================================
# Presets & Slicing Methods
# ============================================================================

SLICING_METHODS = {
    "cut0": "No slice / 不切",
    "cut1": "Slice by 4 sentences / 凑四句切",
    "cut2": "Slice by 50 characters / 凑50字切",
    "cut3": "Slice by Chinese punctuation / 按中文句号。切",
    "cut4": "Slice by English punctuation / 按英文句号.切",
    "cut5": "Slice by punctuation / 按标点符号切",
}

TTS_PRESETS: Dict[str, Dict[str, Any]] = {
    "high_quality": {
        "name": "High Quality",
        "speed": 0.9,
        "speed_factor": 0.9,
        "top_k": 20,
        "top_p": 1.0,
        "temperature": 0.8,
        "text_split_method": "cut5",
        "streaming_mode": 1,
        "batch_size": 1,
    },
    "balanced": {
        "name": "Balanced",
        "speed": 1.0,
        "speed_factor": 1.0,
        "top_k": 15,
        "top_p": 1.0,
        "temperature": 1.0,
        "text_split_method": "cut5",
        "streaming_mode": 2,
        "batch_size": 1,
    },
    "low_latency": {
        "name": "Low Latency",
        "speed": 1.2,
        "speed_factor": 1.2,
        "top_k": 5,
        "top_p": 0.9,
        "temperature": 0.5,
        "text_split_method": "cut5",
        "streaming_mode": 3,
        "batch_size": 1,
    },
}


def resolve_tts_options(options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Merges preset defaults with user-supplied TTS inference options.
    Normalizes parameter keys to official GPT-SoVITS api_v2.py format
    (speed_factor, text_lang, prompt_lang, text_split_method, streaming_mode).
    """
    options = options or {}
    preset_key = str(options.get("preset", "balanced")).lower().replace(" ", "_")
    base_params = dict(TTS_PRESETS.get(preset_key, TTS_PRESETS["balanced"]))

    speed_val = options.get("speed_factor", options.get("speed", base_params.get("speed_factor", 1.0)))
    text_lang_val = str(options.get("text_lang", options.get("text_language", "ja")))
    prompt_lang_val = str(options.get("prompt_lang", options.get("prompt_language", options.get("refer_language", "ja"))))
    split_val = str(options.get("text_split_method", options.get("how_to_cut", options.get("cut_option", base_params.get("text_split_method", "cut5")))))
    streaming_val = int(options.get("streaming_mode", base_params.get("streaming_mode", 2)))

    merged = {
        "speed_factor": float(speed_val),
        "speed": float(speed_val),  # keep alias for backwards compatibility
        "top_k": int(options.get("top_k", base_params.get("top_k", 15))),
        "top_p": float(options.get("top_p", base_params.get("top_p", 1.0))),
        "temperature": float(options.get("temperature", base_params.get("temperature", 1.0))),
        "text_lang": text_lang_val,
        "text_language": text_lang_val,
        "prompt_lang": prompt_lang_val,
        "prompt_language": prompt_lang_val,
        "text_split_method": split_val,
        "streaming_mode": streaming_val,
        "batch_size": int(options.get("batch_size", base_params.get("batch_size", 1))),
        "seed": int(options.get("seed", -1)),
        "fragment_interval": float(options.get("fragment_interval", 0.3)),
        "ref_audio_path": options.get("ref_audio_path", options.get("refer_audio_path", "")),
        "prompt_text": options.get("prompt_text", options.get("refer_text", "")),
    }

    # Pass through any extra custom options
    for k, v in options.items():
        if k not in merged and k != "preset":
            merged[k] = v

    return merged


# ============================================================================
# Voice Profile Data Representation Helper
# ============================================================================

class VoiceProfileWeightSpec(BaseModel):
    name: str
    gpt_weights_path: str
    sovits_weights_path: str
    refer_audio_path: str
    refer_text: str
    refer_language: str = "ja"
    prompt_language: str = "ja"
    text_language: str = "ja"


def _extract_weight_spec(target: Any) -> VoiceProfileWeightSpec:
    """Extracts weight paths and refer audio fields from various object types."""
    if isinstance(target, dict):
        return VoiceProfileWeightSpec(
            name=target.get("name", "Unnamed"),
            gpt_weights_path=target.get("gpt_weights_path", ""),
            sovits_weights_path=target.get("sovits_weights_path", ""),
            refer_audio_path=target.get("refer_audio_path") or target.get("ref_audio_path") or "",
            refer_text=target.get("refer_text") or target.get("prompt_text") or "",
            refer_language=target.get("refer_language") or target.get("prompt_lang") or "ja",
            prompt_language=target.get("prompt_language") or target.get("prompt_lang") or "ja",
            text_language=target.get("text_language") or target.get("text_lang") or "ja",
        )
    elif hasattr(target, "gpt_weights_path"):
        return VoiceProfileWeightSpec(
            name=getattr(target, "name", "Unnamed"),
            gpt_weights_path=getattr(target, "gpt_weights_path", ""),
            sovits_weights_path=getattr(target, "sovits_weights_path", ""),
            refer_audio_path=getattr(target, "refer_audio_path", getattr(target, "ref_audio_path", "")),
            refer_text=getattr(target, "refer_text", getattr(target, "prompt_text", "")),
            refer_language=getattr(target, "refer_language", getattr(target, "prompt_lang", "ja")),
            prompt_language=getattr(target, "prompt_language", getattr(target, "prompt_lang", "ja")),
            text_language=getattr(target, "text_language", getattr(target, "text_lang", "ja")),
        )
    else:
        raise ValueError(f"Cannot extract weight spec from object of type {type(target)}")


# ============================================================================
# GPT-SoVITS Client
# ============================================================================

class GptSovitsClient:
    """
    Asynchronous client for GPT-SoVITS API service.
    Implements endpoints:
      - /set_gpt_weights
      - /set_sovits_weights
      - /set_refer_audio
      - /tts
      - /control
      - /health, /openapi.json, /docs

    Features:
      - Thread-safe asyncio.Lock inference mutex serializing TTS and model switching.
      - 3-step transactional switching with automatic rollback to previous weights on failure.
      - Stage cues parentheses cleaning before synthesis.
      - Streaming TTS generator yielding binary audio chunks.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:9880",
        timeout: float = 30.0,
        client: Optional[httpx.AsyncClient] = None,
        server: Optional[Any] = None,
    ):
        self.base_url = str(base_url).rstrip("/")
        self.timeout = timeout
        self._client = client
        self.server = server  # Supports MockGptSovitsServer in tests
        self.lock = asyncio.Lock()

        # State tracking
        self.active_profile: Optional[Any] = None
        self.is_switching: bool = False
        self.current_gpt_weights: Optional[str] = None
        self.current_sovits_weights: Optional[str] = None
        self.current_refer_audio: Optional[str] = None
        self.current_refer_text: Optional[str] = None
        self.current_refer_language: Optional[str] = None

    async def _request(
        self,
        method: str,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        """Internal HTTP request dispatcher supporting direct mock server or httpx."""
        if self.server and hasattr(self.server, "handle_request"):
            return await self.server.handle_request(method, path, json_data=json_data, params=params)

        url = f"{self.base_url}{path}"
        if self._client:
            return await self._client.request(
                method, url, json=json_data, params=params, timeout=self.timeout
            )
        else:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                return await client.request(
                    method, url, json=json_data, params=params
                )

    # ------------------------------------------------------------------------
    # Health & Diagnostic Endpoints
    # ------------------------------------------------------------------------

    async def check_health(self) -> Dict[str, Any]:
        """Probes GPT-SoVITS backend reachability."""
        try:
            resp = await self._request("GET", "/control")
            if resp.status_code == 200:
                return {
                    "connected": True,
                    "status": "running",
                    "url": self.base_url,
                    "current_gpt_weights": self.current_gpt_weights,
                    "current_sovits_weights": self.current_sovits_weights,
                }
        except Exception:
            pass

        try:
            resp = await self._request("GET", "/")
            if resp.status_code == 200:
                return {
                    "connected": True,
                    "status": "running",
                    "url": self.base_url,
                    "current_gpt_weights": self.current_gpt_weights,
                    "current_sovits_weights": self.current_sovits_weights,
                }
        except Exception as exc:
            return {
                "connected": False,
                "status": "unreachable",
                "url": self.base_url,
                "error": str(exc),
            }

        return {
            "connected": False,
            "status": "unreachable",
            "url": self.base_url,
        }

    async def control(self, command: str = "restart") -> Dict[str, Any]:
        """Sends control command to GPT-SoVITS service."""
        resp = await self._request("POST", "/control", json_data={"command": command})
        if resp.status_code == 200:
            return resp.json()
        raise RuntimeError(f"Control command failed with status {resp.status_code}: {resp.text}")

    # ------------------------------------------------------------------------
    # Individual Weight Endpoints
    # ------------------------------------------------------------------------

    async def set_gpt_weights(self, weights_path: str) -> bool:
        """Sets GPT weights path (GET /set_gpt_weights?weights_path=...)."""
        resp = await self._request("GET", "/set_gpt_weights", params={"weights_path": weights_path})
        if resp.status_code == 200:
            self.current_gpt_weights = weights_path
            return True
        logger.error("Failed to set GPT weights (%s): HTTP %d %s", weights_path, resp.status_code, resp.text)
        return False

    async def set_sovits_weights(self, weights_path: str) -> bool:
        """Sets SoVITS weights path (GET /set_sovits_weights?weights_path=...)."""
        resp = await self._request("GET", "/set_sovits_weights", params={"weights_path": weights_path})
        if resp.status_code == 200:
            self.current_sovits_weights = weights_path
            return True
        logger.error("Failed to set SoVITS weights (%s): HTTP %d %s", weights_path, resp.status_code, resp.text)
        return False

    async def set_refer_audio(
        self,
        refer_audio_path: str,
        refer_text: str = "",
        refer_language: str = "ja",
    ) -> bool:
        """Sets reference audio (GET /set_refer_audio?refer_audio_path=...)."""
        resp = await self._request("GET", "/set_refer_audio", params={"refer_audio_path": refer_audio_path})
        if resp.status_code == 200:
            self.current_refer_audio = refer_audio_path
            self.current_refer_text = refer_text
            self.current_refer_language = refer_language
            return True
        logger.error("Failed to set refer audio (%s): HTTP %d %s", refer_audio_path, resp.status_code, resp.text)
        return False

    # ------------------------------------------------------------------------
    # 3-Step Atomic Model Switching with Auto-Rollback
    # ------------------------------------------------------------------------

    async def switch_voice_profile(self, target: Any) -> bool:
        """
        Switches GPT-SoVITS voice profile in 3 transactional steps:
          Step 1: GET /set_gpt_weights?weights_path=...
          Step 2: GET /set_sovits_weights?weights_path=...
          Step 3: GET /set_refer_audio?refer_audio_path=...

        If any step fails, automatically rolls back previous steps to restore
        the prior working state. Mutex protected with asyncio.Lock.
        """
        async with self.lock:
            self.is_switching = True
            spec = _extract_weight_spec(target)
            prev_profile = self.active_profile
            prev_spec = _extract_weight_spec(prev_profile) if prev_profile else None

            logger.info("Switching voice profile to '%s' (GPT: %s, SoVITS: %s)...",
                        spec.name, spec.gpt_weights_path, spec.sovits_weights_path)

            try:
                # Step 1: Set GPT Weights
                r1 = await self._request("GET", "/set_gpt_weights", params={"weights_path": spec.gpt_weights_path})
                if r1.status_code != 200:
                    logger.error("Switch failed at Step 1 (GPT weights): %s", r1.text)
                    return False
                self.current_gpt_weights = spec.gpt_weights_path

                # Step 2: Set SoVITS Weights
                r2 = await self._request("GET", "/set_sovits_weights", params={"weights_path": spec.sovits_weights_path})
                if r2.status_code != 200:
                    logger.error("Switch failed at Step 2 (SoVITS weights): %s. Initiating rollback...", r2.text)
                    # Rollback Step 1
                    if prev_spec and prev_spec.gpt_weights_path:
                        await self._request("GET", "/set_gpt_weights", params={"weights_path": prev_spec.gpt_weights_path})
                        self.current_gpt_weights = prev_spec.gpt_weights_path
                    return False
                self.current_sovits_weights = spec.sovits_weights_path

                # Step 3: Set Reference Audio
                r3 = await self._request("GET", "/set_refer_audio", params={"refer_audio_path": spec.refer_audio_path})
                if r3.status_code != 200:
                    logger.error("Switch failed at Step 3 (Refer Audio): %s. Initiating rollback...", r3.text)
                    # Rollback Step 2 & Step 1
                    if prev_spec:
                        if prev_spec.sovits_weights_path:
                            await self._request("GET", "/set_sovits_weights", params={"weights_path": prev_spec.sovits_weights_path})
                            self.current_sovits_weights = prev_spec.sovits_weights_path
                        if prev_spec.gpt_weights_path:
                            await self._request("GET", "/set_gpt_weights", params={"weights_path": prev_spec.gpt_weights_path})
                            self.current_gpt_weights = prev_spec.gpt_weights_path
                        if prev_spec.refer_audio_path:
                            await self._request("GET", "/set_refer_audio", params={"refer_audio_path": prev_spec.refer_audio_path})
                    return False

                self.current_refer_audio = spec.refer_audio_path
                self.current_refer_text = spec.refer_text
                self.current_refer_language = spec.refer_language
                self.active_profile = target
                logger.info("Successfully switched voice profile to '%s'", spec.name)
                return True

            except Exception as exc:
                logger.error("Exception during voice profile switch: %s. Rolling back...", exc, exc_info=True)
                if prev_spec:
                    try:
                        if prev_spec.sovits_weights_path:
                            await self._request("GET", "/set_sovits_weights", params={"weights_path": prev_spec.sovits_weights_path})
                        if prev_spec.gpt_weights_path:
                            await self._request("GET", "/set_gpt_weights", params={"weights_path": prev_spec.gpt_weights_path})
                        if prev_spec.refer_audio_path:
                            await self._request("GET", "/set_refer_audio", params={"refer_audio_path": prev_spec.refer_audio_path})
                    except Exception:
                        pass
                return False
            finally:
                self.is_switching = False

    # ------------------------------------------------------------------------
    # Synthesis Endpoints (/tts)
    # ------------------------------------------------------------------------

    def _build_tts_payload(self, text: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Builds standardized GPT-SoVITS official /tts request payload."""
        resolved = resolve_tts_options(options)
        ref_audio = resolved.get("ref_audio_path") or self.current_refer_audio or ""
        ref_text = resolved.get("prompt_text") or self.current_refer_text or ""
        ref_lang = resolved.get("prompt_lang") or self.current_refer_language or "ja"

        payload = {
            "text": text,
            "text_lang": resolved["text_lang"],
            "ref_audio_path": ref_audio,
            "prompt_text": ref_text,
            "prompt_lang": ref_lang,
            "top_k": resolved["top_k"],
            "top_p": resolved["top_p"],
            "temperature": resolved["temperature"],
            "text_split_method": resolved["text_split_method"],
            "batch_size": resolved["batch_size"],
            "speed_factor": resolved["speed_factor"],
            "streaming_mode": resolved["streaming_mode"],
            "seed": resolved["seed"],
        }

        # Pass through any additional options
        for k, v in (options or {}).items():
            if k not in payload and k != "preset":
                payload[k] = v

        return payload

    async def synthesize(self, text: str, options: Optional[Dict[str, Any]] = None) -> bytes:
        """
        Synthesizes text into complete WAV audio bytes.
        Cleans Japanese stage cues before synthesis.
        Guarded by inference mutex lock.
        """
        async with self.lock:
            cleaned_text = clean_japanese_parentheses(text)
            if not cleaned_text:
                raise ValueError("Text is empty after cleaning stage directions")

            opts = dict(options or {})
            opts["streaming_mode"] = False
            payload = self._build_tts_payload(cleaned_text, opts)
            payload["streaming_mode"] = False
            resp = await self._request("POST", "/tts", json_data=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"TTS synthesis failed with status {resp.status_code}: {resp.text}")

            return resp.content

    async def stream_tts(
        self,
        text: str,
        options: Optional[Dict[str, Any]] = None,
        chunk_size: int = 4096,
    ) -> AsyncGenerator[bytes, None]:
        """
        Streams synthesized audio in binary chunks.
        Cleans Japanese stage cues before synthesis.
        Guarded by inference mutex lock.
        """
        async with self.lock:
            cleaned_text = clean_japanese_parentheses(text)
            if not cleaned_text:
                raise ValueError("Text is empty after cleaning stage directions")

            opts = dict(options or {})
            opts["streaming_mode"] = True
            payload = self._build_tts_payload(cleaned_text, opts)
            payload["streaming_mode"] = True

            if self.server and hasattr(self.server, "handle_request"):
                resp = await self.server.handle_request("POST", "/tts", json_data=payload)
                if resp.status_code != 200:
                    raise RuntimeError(f"TTS synthesis failed with status {resp.status_code}: {resp.text}")
                content = resp.content
                for i in range(0, len(content), chunk_size):
                    yield content[i:i + chunk_size]
            else:
                url = f"{self.base_url}/tts"
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    async with client.stream("POST", url, json=payload) as resp:
                        if resp.status_code != 200:
                            err_bytes = await resp.aread()
                            raise RuntimeError(
                                f"TTS synthesis failed with status {resp.status_code}: {err_bytes.decode('utf-8', errors='ignore')}"
                            )
                        async for chunk in resp.aiter_bytes(chunk_size=chunk_size):
                            if chunk:
                                yield chunk

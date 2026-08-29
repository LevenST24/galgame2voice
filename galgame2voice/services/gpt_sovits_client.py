"""
GPT-SoVITS API Client for galgame2voice.

Single shared async client with:
  - Persistent httpx connection pool (keep-alive, no per-request TCP churn)
  - Tiered timeouts (fast connect fail, long GPU inference read)
  - Transient-failure retry for TTS synthesis
  - asyncio.Lock inference mutex shared across the whole application
  - 3-step atomic model switching with rollback
  - Hot base_url reload (settings console changes take effect immediately)
"""

import asyncio
import logging
import re
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
from pydantic import BaseModel

logger = logging.getLogger("galgame2voice.services.gpt_sovits_client")


# ============================================================================
# Japanese Parentheses Cleaner (Stage Direction Stripper)
# ============================================================================

def clean_japanese_parentheses(text: str, max_passes: int = 5) -> str:
    """
    Strips stage cues and action directions enclosed in fullwidth （...） or ASCII (...) parentheses.
    Applies multi-pass regex sanitization (up to max_passes) to handle nested brackets like （（ため息））.
    """
    if not text:
        return ""

    cleaned = text
    for _ in range(max_passes):
        prev = cleaned
        cleaned = re.sub(r'（[^（）]*）', '', cleaned)
        cleaned = re.sub(r'\([^()]*\)', '', cleaned)
        if cleaned == prev:
            break

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
        "batch_size": 1,
    },
}


def resolve_tts_options(options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Merges preset defaults with user-supplied TTS inference options.
    Normalizes parameter keys to official GPT-SoVITS api_v2.py format.
    """
    options = options or {}
    preset_key = str(options.get("preset", "balanced")).lower().replace(" ", "_")
    base_params = dict(TTS_PRESETS.get(preset_key, TTS_PRESETS["balanced"]))

    speed_val = options.get("speed_factor", options.get("speed", base_params.get("speed_factor", 1.0)))
    text_lang_val = str(options.get("text_lang", options.get("text_language", "ja")))
    prompt_lang_val = str(options.get("prompt_lang", options.get("prompt_language", options.get("refer_language", "ja"))))
    split_val = str(options.get("text_split_method", options.get("how_to_cut", options.get("cut_option", base_params.get("text_split_method", "cut5")))))

    merged = {
        "speed_factor": float(speed_val),
        "speed": float(speed_val),  # alias kept for backwards compatibility
        "top_k": int(options.get("top_k", base_params.get("top_k", 15))),
        "top_p": float(options.get("top_p", base_params.get("top_p", 1.0))),
        "temperature": float(options.get("temperature", base_params.get("temperature", 1.0))),
        "text_lang": text_lang_val,
        "text_language": text_lang_val,  # alias
        "prompt_lang": prompt_lang_val,
        "prompt_language": prompt_lang_val,  # alias
        "text_split_method": split_val,
        "batch_size": int(options.get("batch_size", base_params.get("batch_size", 1))),
        "seed": int(options.get("seed", -1)),
        "fragment_interval": float(options.get("fragment_interval", 0.3)),
        "ref_audio_path": options.get("ref_audio_path", options.get("refer_audio_path", "")),
        "prompt_text": options.get("prompt_text", options.get("refer_text", "")),
    }

    # streaming_mode: accept bool or int (1/2/3 presets), normalized to bool later.
    streaming_raw = options.get("streaming_mode", options.get("stream_mode"))
    if streaming_raw is not None:
        merged["streaming_mode"] = streaming_raw

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

# Tiered timeout profile: fail fast on connect, allow long GPU synthesis reads.
# NOTE: connect is capped at 1s because some VPN/TUN proxy stacks delay even
# loopback connection-refused to ~2s; a healthy local engine connects in <50ms.
TTS_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=15.0, pool=15.0)
SWITCH_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=15.0, pool=15.0)
HEALTH_TIMEOUT = httpx.Timeout(connect=1.0, read=2.5, write=2.5, pool=2.5)


class GptSovitsClient:
    """
    Asynchronous client for the GPT-SoVITS api_v2 service.

    One instance = one persistent httpx connection pool + one inference mutex.
    The whole application should share a single instance (see get_gpt_sovits_client)
    so that synthesis and model switching are globally serialized against the
    single GPU inference engine.

    Mock-server mode (`server=`) is preserved for the test suite.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:9880",
        timeout: float = 300.0,
        client: Optional[httpx.AsyncClient] = None,
        server: Optional[Any] = None,
    ):
        self.base_url = str(base_url).rstrip("/")
        self.timeout = timeout
        self._client = client
        self.server = server  # MockGptSovitsServer in tests
        self.lock = asyncio.Lock()

        # State tracking
        self.active_profile: Optional[Any] = None
        self.is_switching: bool = False
        self.current_gpt_weights: Optional[str] = None
        self.current_sovits_weights: Optional[str] = None
        self.current_refer_audio: Optional[str] = None
        self.current_refer_text: Optional[str] = None
        self.current_refer_language: Optional[str] = None

    # ------------------------------------------------------------------
    # Connection pool lifecycle
    # ------------------------------------------------------------------

    def _get_http_client(self) -> httpx.AsyncClient:
        """Lazily creates and reuses a pooled httpx.AsyncClient (keep-alive)."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                trust_env=False,
                timeout=TTS_TIMEOUT,
                limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
            )
        return self._client

    async def aclose(self) -> None:
        """Closes the pooled HTTP client. Safe to call multiple times."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def set_base_url(self, new_url: str) -> None:
        """Hot-reloads the GPT-SoVITS endpoint URL, recreating the connection pool."""
        new_url = str(new_url).strip().rstrip("/")
        if not new_url:
            return
        if new_url == self.base_url:
            return
        logger.info("GPT-SoVITS base URL changing: %s -> %s", self.base_url, new_url)
        self.base_url = new_url
        # Swap the client atomically; close the old pool lazily so any in-flight
        # request still holding it can finish instead of failing mid-stream.
        old_client = self._client
        self._client = None
        if old_client is not None and not old_client.is_closed:
            async def _close_late():
                try:
                    await asyncio.sleep(5.0)
                    await old_client.aclose()
                except Exception:
                    pass
            asyncio.create_task(_close_late())

    async def _request(
        self,
        method: str,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[httpx.Timeout] = None,
    ) -> httpx.Response:
        """Internal HTTP request dispatcher supporting mock server or pooled httpx."""
        if self.server is not None and hasattr(self.server, "handle_request"):
            return await self.server.handle_request(method, path, json_data=json_data, params=params)

        url = f"{self.base_url}{path}"
        client = self._get_http_client()
        return await client.request(
            method, url, json=json_data, params=params, timeout=timeout
        )

    # ------------------------------------------------------------------
    # Health & Diagnostic Endpoints
    # ------------------------------------------------------------------

    async def check_health(self) -> Dict[str, Any]:
        """
        Probes GPT-SoVITS reachability via GET /control (api_v2 control endpoint returns 400 when active).
        HTTP 200/400 proves the engine is alive and listening; other codes / network errors are unreachable.
        Dispatches through the mock server when one is configured (tests).
        """
        try:
            if self.server is not None and hasattr(self.server, "handle_request"):
                try:
                    resp = await self.server.handle_request("GET", "/control")
                except Exception:
                    resp = await self.server.handle_request("GET", "/")
            else:
                client = self._get_http_client()
                url = f"{self.base_url}/control"
                try:
                    resp = await client.get(url, timeout=HEALTH_TIMEOUT)
                except Exception:
                    # Fallback to GET / if /control fails
                    resp = await client.get(f"{self.base_url}/", timeout=HEALTH_TIMEOUT)

            if resp.status_code in (200, 400):
                return {
                    "connected": True,
                    "status": "running",
                    "url": self.base_url,
                    "http_status": resp.status_code,
                    "current_gpt_weights": self.current_gpt_weights,
                    "current_sovits_weights": self.current_sovits_weights,
                }
            return {
                "connected": False,
                "status": "unreachable",
                "url": self.base_url,
                "http_status": resp.status_code,
                "error": f"Unexpected status code: {resp.status_code}",
                "current_gpt_weights": self.current_gpt_weights,
                "current_sovits_weights": self.current_sovits_weights,
            }
        except Exception as exc:
            return {
                "connected": False,
                "status": "unreachable",
                "url": self.base_url,
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def control(self, command: str = "restart") -> Dict[str, Any]:
        """Sends control command to GPT-SoVITS service."""
        resp = await self._request("POST", "/control", json_data={"command": command})
        if resp.status_code == 200:
            return resp.json()
        raise RuntimeError(f"Control command failed with status {resp.status_code}: {resp.text}")

    # ------------------------------------------------------------------
    # Individual Weight Endpoints
    # ------------------------------------------------------------------

    async def set_gpt_weights(self, weights_path: str) -> bool:
        """Sets GPT weights path. Loading onto GPU may take tens of seconds."""
        resp = await self._request("GET", "/set_gpt_weights", params={"weights_path": weights_path}, timeout=SWITCH_TIMEOUT)
        if resp.status_code == 200:
            self.current_gpt_weights = weights_path
            return True
        logger.error("Failed to set GPT weights (%s): HTTP %d %s", weights_path, resp.status_code, resp.text)
        return False

    async def set_sovits_weights(self, weights_path: str) -> bool:
        """Sets SoVITS weights path. Loading onto GPU may take tens of seconds."""
        resp = await self._request("GET", "/set_sovits_weights", params={"weights_path": weights_path}, timeout=SWITCH_TIMEOUT)
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
        """Sets reference audio."""
        resp = await self._request("GET", "/set_refer_audio", params={"refer_audio_path": refer_audio_path})
        if resp.status_code == 200:
            self.current_refer_audio = refer_audio_path
            self.current_refer_text = refer_text
            self.current_refer_language = refer_language
            return True
        logger.error("Failed to set refer audio (%s): HTTP %d %s", refer_audio_path, resp.status_code, resp.text)
        return False

    # ------------------------------------------------------------------
    # 3-Step Atomic Model Switching with Auto-Rollback
    # ------------------------------------------------------------------

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
                r1 = await self._request("GET", "/set_gpt_weights", params={"weights_path": spec.gpt_weights_path}, timeout=SWITCH_TIMEOUT)
                if r1.status_code != 200:
                    logger.error("Switch failed at Step 1 (GPT weights): %s", r1.text)
                    return False
                self.current_gpt_weights = spec.gpt_weights_path

                r2 = await self._request("GET", "/set_sovits_weights", params={"weights_path": spec.sovits_weights_path}, timeout=SWITCH_TIMEOUT)
                if r2.status_code != 200:
                    logger.error("Switch failed at Step 2 (SoVITS weights): %s. Initiating rollback...", r2.text)
                    if prev_spec and prev_spec.gpt_weights_path:
                        await self._request("GET", "/set_gpt_weights", params={"weights_path": prev_spec.gpt_weights_path}, timeout=SWITCH_TIMEOUT)
                        self.current_gpt_weights = prev_spec.gpt_weights_path
                    return False
                self.current_sovits_weights = spec.sovits_weights_path

                r3 = await self._request("GET", "/set_refer_audio", params={"refer_audio_path": spec.refer_audio_path})
                if r3.status_code != 200:
                    logger.error("Switch failed at Step 3 (Refer Audio): %s. Initiating rollback...", r3.text)
                    if prev_spec:
                        if prev_spec.sovits_weights_path:
                            await self._request("GET", "/set_sovits_weights", params={"weights_path": prev_spec.sovits_weights_path}, timeout=SWITCH_TIMEOUT)
                            self.current_sovits_weights = prev_spec.sovits_weights_path
                        if prev_spec.gpt_weights_path:
                            await self._request("GET", "/set_gpt_weights", params={"weights_path": prev_spec.gpt_weights_path}, timeout=SWITCH_TIMEOUT)
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
                            await self._request("GET", "/set_sovits_weights", params={"weights_path": prev_spec.sovits_weights_path}, timeout=SWITCH_TIMEOUT)
                        if prev_spec.gpt_weights_path:
                            await self._request("GET", "/set_gpt_weights", params={"weights_path": prev_spec.gpt_weights_path}, timeout=SWITCH_TIMEOUT)
                        if prev_spec.refer_audio_path:
                            await self._request("GET", "/set_refer_audio", params={"refer_audio_path": prev_spec.refer_audio_path})
                    except Exception as rollback_exc:
                        # Rollback failure leaves server state diverged from local state — surface it loudly.
                        logger.error("ROLLBACK FAILED after switch error (server state may diverge): %s", rollback_exc)
                return False
            finally:
                self.is_switching = False

    # ------------------------------------------------------------------
    # Synthesis Endpoints (/tts)
    # ------------------------------------------------------------------

    def _build_tts_payload(self, text: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Builds standardized GPT-SoVITS official /tts request payload."""
        resolved = resolve_tts_options(options)
        ref_audio = resolved.get("ref_audio_path") or self.current_refer_audio or ""
        ref_text = resolved.get("prompt_text") or self.current_refer_text or ""
        ref_lang = resolved.get("prompt_lang") or self.current_refer_language or "ja"

        return {
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
            "streaming_mode": resolved.get("streaming_mode", False),
            "seed": resolved["seed"],
        }

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """Heuristic: network-level errors are transient; HTTP 4xx are not."""
        if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout,
                            httpx.PoolTimeout, httpx.RemoteProtocolError)):
            return True
        if isinstance(exc, RuntimeError) and "status 5" in str(exc):
            return True
        return False

    async def synthesize(
        self,
        text: str,
        options: Optional[Dict[str, Any]] = None,
        retries: int = 1,
    ) -> bytes:
        """
        Synthesizes text into complete WAV audio bytes.
        Cleans Japanese stage cues before synthesis.
        Guarded by the shared inference mutex lock.
        Retries once on transient network failures.
        """
        async with self.lock:
            cleaned_text = clean_japanese_parentheses(text)
            if not cleaned_text:
                raise ValueError("Text is empty after cleaning stage directions")

            opts = dict(options or {})
            opts["streaming_mode"] = False
            payload = self._build_tts_payload(cleaned_text, opts)
            payload["streaming_mode"] = False

            attempt = 0
            while True:
                attempt += 1
                try:
                    resp = await self._request("POST", "/tts", json_data=payload)
                    if resp.status_code != 200:
                        raise RuntimeError(f"TTS synthesis failed with status {resp.status_code}: {resp.text[:300]}")
                    if not resp.content:
                        raise RuntimeError("TTS synthesis returned empty audio payload")
                    return resp.content
                except Exception as exc:
                    if attempt <= retries and self._is_transient(exc):
                        logger.warning(
                            "TTS transient failure (attempt %d/%d) for '%s...': %s — retrying",
                            attempt, attempt + retries, cleaned_text[:20], exc,
                        )
                        await asyncio.sleep(0.8 * attempt)
                        continue
                    raise

    async def stream_tts(
        self,
        text: str,
        options: Optional[Dict[str, Any]] = None,
        chunk_size: int = 4096,
    ) -> AsyncGenerator[bytes, None]:
        """
        Streams synthesized audio in binary chunks.
        The inference lock is held while the HTTP stream is open; abandoning the
        generator (GeneratorExit) releases both the connection and the lock.
        """
        async with self.lock:
            cleaned_text = clean_japanese_parentheses(text)
            if not cleaned_text:
                raise ValueError("Text is empty after cleaning stage directions")

            opts = dict(options or {})
            opts["streaming_mode"] = True
            payload = self._build_tts_payload(cleaned_text, opts)
            payload["streaming_mode"] = True

            if self.server is not None and hasattr(self.server, "handle_request"):
                resp = await self.server.handle_request("POST", "/tts", json_data=payload)
                if resp.status_code != 200:
                    raise RuntimeError(f"TTS synthesis failed with status {resp.status_code}: {resp.text}")
                content = resp.content
                for i in range(0, len(content), chunk_size):
                    yield content[i:i + chunk_size]
            else:
                url = f"{self.base_url}/tts"
                client = self._get_http_client()
                async with client.stream("POST", url, json=payload, timeout=TTS_TIMEOUT) as resp:
                    if resp.status_code != 200:
                        err_bytes = await resp.aread()
                        raise RuntimeError(
                            f"TTS synthesis failed with status {resp.status_code}: {err_bytes.decode('utf-8', errors='ignore')[:300]}"
                        )
                    async for chunk in resp.aiter_bytes(chunk_size=chunk_size):
                        if chunk:
                            yield chunk


# ============================================================================
# Application-Level Singleton
# ============================================================================

_global_gpt_sovits_client: Optional[GptSovitsClient] = None


def get_gpt_sovits_client() -> GptSovitsClient:
    """
    Returns the application-wide singleton GptSovitsClient.
    All services (TtsService, VoiceManager, Telegram, routers) MUST share this
    instance so the inference mutex actually serializes GPU access globally.
    """
    global _global_gpt_sovits_client
    if _global_gpt_sovits_client is None:
        settings = None
        try:
            from galgame2voice.config import get_settings
            settings = get_settings()
        except Exception:
            pass
        base_url = settings.gpt_sovits_base_url if settings else "http://127.0.0.1:9880"
        _global_gpt_sovits_client = GptSovitsClient(base_url=base_url)
    return _global_gpt_sovits_client


async def reload_gpt_sovits_client_base_url(new_url: str) -> None:
    """Hot-updates the singleton's endpoint (called when settings console saves gpt_sovits_url)."""
    client = get_gpt_sovits_client()
    await client.set_base_url(new_url)


def set_gpt_sovits_client(client: Optional[GptSovitsClient]) -> None:
    """Replaces or resets the singleton (used by tests)."""
    global _global_gpt_sovits_client
    _global_gpt_sovits_client = client


async def close_gpt_sovits_client() -> None:
    """Closes the singleton's connection pool (called during app shutdown)."""
    global _global_gpt_sovits_client
    if _global_gpt_sovits_client is not None:
        await _global_gpt_sovits_client.aclose()
        _global_gpt_sovits_client = None


__all__ = [
    "GptSovitsClient",
    "get_gpt_sovits_client",
    "set_gpt_sovits_client",
    "reload_gpt_sovits_client_base_url",
    "close_gpt_sovits_client",
    "clean_japanese_parentheses",
    "resolve_tts_options",
    "SLICING_METHODS",
    "TTS_PRESETS",
]

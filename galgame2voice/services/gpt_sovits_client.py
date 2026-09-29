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
import os
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional, Union

import httpx

from galgame2voice.utils.audio_spec import (
    PCM_32KHZ_16BIT_MONO_BYTE_RATE,
    REFERENCE_AUDIO_MAX_SECONDS,
    REFERENCE_AUDIO_MIN_SECONDS,
    SILENT_AUDIO_ERROR,
    AudioSpec,
    AudioSpecCache,
    _AUDIO_SPEC_CACHE,
    _safe_resolve_path,
    async_probe_audio_duration_seconds,
    extract_wav_duration,
    is_silent_audio,
    probe_audio_duration_seconds,
    probe_audio_spec,
    resolve_reference_audio_path,
    validate_reference_audio,
    wav_is_silent,
    wav_peak_amplitude,
)
from galgame2voice.services.tts_options import (
    SLICING_METHODS,
    TTS_PRESETS,
    VoiceProfileWeightSpec,
    _TTS_NUMERIC_RANGES,
    _TTS_STRING_MAXLEN,
    _extract_weight_spec,
    resolve_tts_options,
    validate_user_tts_options,
)
from galgame2voice.utils.text_splitter import normalize_dialogue_prosody
from galgame2voice.utils.prosody import (
    DYNAMIC_SPEED_MIN,
    DYNAMIC_SPEED_MAX,
    DYNAMIC_TEMP_MIN,
    DYNAMIC_TEMP_MAX,
    clamp_dynamic_speed,
    clamp_dynamic_temperature,
)
from galgame2voice.services.dynamic_batcher import (
    get_speed_tracker,
    get_batch_scheduler,
)
from galgame2voice.utils.japanese_phonetics import (
    clean_japanese_parentheses,
    extract_stage_directions_and_emotion,
    normalize_japanese_for_tts,
)

logger = logging.getLogger("galgame2voice.services.gpt_sovits_client")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


# Backward-compatibility alias
_GLOBAL_AUDIO_SPEC_CACHE = _AUDIO_SPEC_CACHE


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

        # In-flight request tracking for hot URL swaps: the old connection
        # pool is closed once in-flight requests drain or the grace period
        # expires, whichever comes first (read timeout is up to 300s).
        self._inflight_requests = 0
        self._close_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Connection pool lifecycle
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        """Lazily creates and reuses a pooled httpx.AsyncClient (keep-alive)."""
        if hasattr(self, "_http_client") and self._http_client is not None:
            return self._http_client
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                trust_env=False,
                timeout=TTS_TIMEOUT,
                limits=httpx.Limits(
                    max_keepalive_connections=20,
                    max_connections=50,
                    keepalive_expiry=30.0,
                ),
            )
        return self._client

    # Backward compatibility alias
    _get_http_client = _get_client

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
        # Swap the client atomically; drain in-flight requests (or give up
        # after a grace period) before closing the old pool so ongoing
        # synthesis streams are not cut off mid-read.
        old_client = self._client
        self._client = None
        if old_client is not None and not old_client.is_closed:
            # The grace period must be no shorter than the TTS read timeout:
            # a legitimate slow synthesis can hold a streaming response open
            # for up to TTS_TIMEOUT.read seconds; force-closing earlier would
            # cut off in-flight streams mid-read (RemoteProtocolError).
            min_grace = float(TTS_TIMEOUT.read) + 30.0
            grace = max(
                float(os.getenv("GALGAME2VOICE_CLIENT_CLOSE_GRACE_SECONDS", "30") or 30),
                min_grace,
            )

            async def _close_when_drained():
                force_closed = False
                try:
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + grace
                    while self._inflight_requests > 0 and loop.time() < deadline:
                        await asyncio.sleep(0.25)
                    force_closed = self._inflight_requests > 0
                except asyncio.CancelledError:
                    force_closed = True
                except Exception as wait_err:
                    logger.debug("Error while waiting for connection pool to drain: %s", wait_err)
                if force_closed:
                    logger.warning(
                        "Force-closing stale GPT-SoVITS connection pool after %.0fs grace: "
                        "%d request(s) still in flight; their streams may be interrupted.",
                        grace,
                        self._inflight_requests,
                    )
                try:
                    await old_client.aclose()
                except Exception as close_err:
                    logger.debug("Non-critical: error closing old GPT-SoVITS client: %s", close_err)

            # Keep a strong reference so the task cannot be garbage collected.
            if self._close_task is not None and not self._close_task.done():
                self._close_task.cancel()
            self._close_task = asyncio.create_task(_close_when_drained())

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
        p = Path(refer_audio_path)
        if not p.is_file() and (_PROJECT_ROOT / refer_audio_path).is_file():
            refer_audio_path = str((_PROJECT_ROOT / refer_audio_path).resolve())
        elif p.is_file():
            refer_audio_path = str(p.resolve())

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

    async def _rollback_weights(
        self,
        prev_spec: Optional[VoiceProfileWeightSpec],
        current_spec: Optional[VoiceProfileWeightSpec] = None,
        rollback_sovits: bool = False,
        rollback_gpt: bool = False,
        rollback_refer: bool = False,
    ) -> None:
        """Rolls back GPT-SoVITS server weights and reference audio to previous spec."""
        if not prev_spec:
            return
        if rollback_sovits and prev_spec.sovits_weights_path:
            if not current_spec or prev_spec.sovits_weights_path != current_spec.sovits_weights_path:
                await self._request("GET", "/set_sovits_weights", params={"weights_path": prev_spec.sovits_weights_path}, timeout=SWITCH_TIMEOUT)
                self.current_sovits_weights = prev_spec.sovits_weights_path
        if rollback_gpt and prev_spec.gpt_weights_path:
            if not current_spec or prev_spec.gpt_weights_path != current_spec.gpt_weights_path:
                await self._request("GET", "/set_gpt_weights", params={"weights_path": prev_spec.gpt_weights_path}, timeout=SWITCH_TIMEOUT)
                self.current_gpt_weights = prev_spec.gpt_weights_path
        if rollback_refer and prev_spec.refer_audio_path:
            rollback_ref = resolve_reference_audio_path(prev_spec.refer_audio_path)
            await self._request("GET", "/set_refer_audio", params={"refer_audio_path": rollback_ref})

    async def switch_voice_profile(self, target: Any, force: bool = False) -> bool:
        """
        Switches GPT-SoVITS voice profile in 3 transactional steps:
          Step 1: GET /set_gpt_weights?weights_path=... (skipped if identical weights already loaded)
          Step 2: GET /set_sovits_weights?weights_path=... (skipped if identical weights already loaded)
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
                # Step 1: GPT weights (skip if identical weights already loaded and not force)
                needs_gpt_update = force or not self.current_gpt_weights or self.current_gpt_weights != spec.gpt_weights_path
                if needs_gpt_update:
                    r1 = await self._request("GET", "/set_gpt_weights", params={"weights_path": spec.gpt_weights_path}, timeout=SWITCH_TIMEOUT)
                    if r1.status_code != 200:
                        logger.error("Switch failed at Step 1 (GPT weights): %s", r1.text)
                        return False
                    self.current_gpt_weights = spec.gpt_weights_path
                else:
                    logger.debug("Skipping /set_gpt_weights: '%s' already loaded", spec.gpt_weights_path)

                # Step 2: SoVITS weights (skip if identical weights already loaded and not force)
                needs_sovits_update = force or not self.current_sovits_weights or self.current_sovits_weights != spec.sovits_weights_path
                if needs_sovits_update:
                    r2 = await self._request("GET", "/set_sovits_weights", params={"weights_path": spec.sovits_weights_path}, timeout=SWITCH_TIMEOUT)
                    if r2.status_code != 200:
                        logger.error("Switch failed at Step 2 (SoVITS weights): %s. Initiating rollback...", r2.text)
                        await self._rollback_weights(prev_spec, spec, rollback_gpt=True)
                        return False
                    self.current_sovits_weights = spec.sovits_weights_path
                else:
                    logger.debug("Skipping /set_sovits_weights: '%s' already loaded", spec.sovits_weights_path)

                # Step 3: Reference Audio
                resolved_ref_audio = resolve_reference_audio_path(spec.refer_audio_path)
                is_same_refer = bool(
                    self.current_refer_audio
                    and self.current_refer_audio == resolved_ref_audio
                    and self.current_refer_text == spec.refer_text
                    and self.current_refer_language == spec.refer_language
                )
                if force or not is_same_refer:
                    r3 = await self._request("GET", "/set_refer_audio", params={"refer_audio_path": resolved_ref_audio})
                    if r3.status_code != 200:
                        logger.error("Switch failed at Step 3 (Refer Audio): %s. Initiating rollback...", r3.text)
                        await self._rollback_weights(prev_spec, spec, rollback_sovits=True, rollback_gpt=True, rollback_refer=True)
                        return False

                self.current_refer_audio = resolved_ref_audio
                self.current_refer_text = spec.refer_text
                self.current_refer_language = spec.refer_language
                self.active_profile = target
                logger.info("Successfully switched voice profile to '%s'", spec.name)
                return True

            except Exception as exc:
                logger.error("Exception during voice profile switch: %s. Rolling back...", exc, exc_info=True)
                if prev_spec:
                    try:
                        await self._rollback_weights(prev_spec, rollback_sovits=True, rollback_gpt=True, rollback_refer=True)
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

        if not ref_audio:
            raise ValueError(
                "No reference audio provided and no active character profile configured. "
                "Please configure an active character package or supply valid reference audio."
            )

        ok, reason = validate_reference_audio(ref_audio)
        if not ok:
            logger.warning(
                "Reference audio '%s' validation warning: %s. Proceeding with specified path.",
                ref_audio,
                reason,
            )

        p = Path(ref_audio)
        if not p.is_file() and (_PROJECT_ROOT / ref_audio).is_file():
            ref_audio = str(_safe_resolve_path(_PROJECT_ROOT / ref_audio))
        elif p.is_file():
            ref_audio = str(_safe_resolve_path(p))

        norm_text = normalize_dialogue_prosody(text) or text

        # Latency-driven dynamic batch size calculation
        user_batch_size = (options or {}).get("batch_size")
        is_streaming = bool(resolved.get("streaming_mode", False))
        final_batch_size = get_batch_scheduler().compute_batch_size(
            text=norm_text,
            is_streaming=is_streaming,
            split_method=resolved["text_split_method"],
            user_batch_size=user_batch_size,
        )

        return {
            "text": norm_text,
            "text_lang": resolved["text_lang"],
            "ref_audio_path": ref_audio,
            "prompt_text": ref_text,
            "prompt_lang": ref_lang,
            "top_k": resolved["top_k"],
            "top_p": resolved["top_p"],
            "temperature": resolved["temperature"],
            "text_split_method": resolved["text_split_method"],
            "fragment_interval": resolved.get("fragment_interval", 0.3),
            "batch_size": final_batch_size,
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
        return isinstance(exc, RuntimeError) and "status 5" in str(exc)

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
            cleaned_text = normalize_japanese_for_tts(text)
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
                    t_synth_start = time.perf_counter()
                    self._inflight_requests += 1
                    try:
                        resp = await self._request("POST", "/tts", json_data=payload)
                    finally:
                        self._inflight_requests -= 1
                    elapsed_s = time.perf_counter() - t_synth_start
                    if resp.status_code != 200:
                        raise RuntimeError(f"TTS synthesis failed with status {resp.status_code}: {resp.text[:300]}")
                    if not resp.content:
                        raise RuntimeError("TTS synthesis returned empty audio payload")
                    if wav_is_silent(resp.content):
                        raise RuntimeError(SILENT_AUDIO_ERROR)

                    dur_est = extract_wav_duration(resp.content)
                    try:
                        get_speed_tracker().record(
                            char_count=len(cleaned_text),
                            elapsed_s=elapsed_s,
                            audio_dur_s=dur_est,
                        )
                    except Exception as exc:
                        logger.debug("Speed tracker record error: %s", exc)

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
        Streams synthesized audio in binary chunks with true end-to-end pipelining.

        Audio chunks are streamed from upstream GPT-SoVITS directly to the caller via an
        async queue as soon as they arrive over HTTP, without waiting for the full response
        to finish buffering. The inference lock is released immediately once the upstream
        HTTP response body is consumed, decoupling slow downstream consumers from the GPU mutex.
        """
        cleaned_text = normalize_japanese_for_tts(text)
        if not cleaned_text:
            raise ValueError("Text is empty after cleaning stage directions")

        opts = dict(options or {})
        opts["streaming_mode"] = True
        payload = self._build_tts_payload(cleaned_text, opts)
        payload["streaming_mode"] = True

        t_stream_start = time.perf_counter()

        # Mock server mode (used in test suite stubs)
        if self.server is not None and hasattr(self.server, "handle_request"):
            async with self.lock:
                resp = await self.server.handle_request("POST", "/tts", json_data=payload)
                if resp.status_code != 200:
                    raise RuntimeError(f"TTS synthesis failed with status {resp.status_code}: {resp.text}")
                audio_bytes = resp.content
                if wav_is_silent(audio_bytes):
                    raise RuntimeError(SILENT_AUDIO_ERROR)
                for i in range(0, len(audio_bytes), chunk_size):
                    yield audio_bytes[i:i + chunk_size]
            return

        # Real GPT-SoVITS engine mode: true upstream -> downstream streaming pipeline
        url = f"{self.base_url}/tts"
        client = self._get_http_client()
        queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        sentinel = object()
        profiler = opts.get("profiler")

        async def _stream_producer():
            try:
                async with self.lock:
                    self._inflight_requests += 1
                    try:
                        async with client.stream("POST", url, json=payload, timeout=TTS_TIMEOUT) as resp:
                            if resp.status_code != 200:
                                err_bytes = await resp.aread()
                                raise RuntimeError(
                                    f"TTS synthesis failed with status {resp.status_code}: {err_bytes.decode('utf-8', errors='ignore')[:300]}"
                                )
                            first_upstream_chunk = True
                            async for chunk in resp.aiter_bytes(chunk_size=chunk_size):
                                if chunk:
                                    if first_upstream_chunk:
                                        if profiler and hasattr(profiler, "record_upstream_first_byte"):
                                            profiler.record_upstream_first_byte()
                                        first_upstream_chunk = False
                                    await queue.put(chunk)
                    finally:
                        self._inflight_requests -= 1
            except BaseException as exc:
                await queue.put(exc)
            finally:
                await queue.put(sentinel)

        producer_task = asyncio.create_task(_stream_producer())
        pre_buffer = bytearray()
        checked_silence = False
        total_bytes_streamed = 0

        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    break
                if isinstance(item, BaseException):
                    raise item

                if not checked_silence:
                    pre_buffer.extend(item)
                    # Inspect initial WAV PCM samples
                    peak = wav_peak_amplitude(bytes(pre_buffer))
                    if peak is not None and peak > 0.0:
                        checked_silence = True
                        if profiler and hasattr(profiler, "record_app_first_chunk"):
                            profiler.record_app_first_chunk()
                        total_bytes_streamed += len(pre_buffer)
                        yield bytes(pre_buffer)
                        pre_buffer.clear()
                    elif len(pre_buffer) >= 32768:
                        raise RuntimeError(SILENT_AUDIO_ERROR)
                else:
                    total_bytes_streamed += len(item)
                    yield item

            if not checked_silence and pre_buffer:
                if wav_is_silent(bytes(pre_buffer)):
                    raise RuntimeError(SILENT_AUDIO_ERROR)
                if profiler and hasattr(profiler, "record_app_first_chunk"):
                    profiler.record_app_first_chunk()
                total_bytes_streamed += len(pre_buffer)
                yield bytes(pre_buffer)

            elapsed_s = time.perf_counter() - t_stream_start
            try:
                get_speed_tracker().record(
                    char_count=len(cleaned_text),
                    elapsed_s=elapsed_s,
                    audio_dur_s=total_bytes_streamed / PCM_32KHZ_16BIT_MONO_BYTE_RATE,
                )
            except Exception as exc:
                logger.debug("Stream speed tracker record error: %s", exc)

        finally:
            if not producer_task.done():
                producer_task.cancel()
                try:
                    await producer_task
                except (asyncio.CancelledError, Exception):
                    pass


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
        except Exception as exc:
            logger.debug("Could not load settings for default GPT-SoVITS URL: %s", exc)
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
    "extract_stage_directions_and_emotion",
    "extract_wav_duration",
    "normalize_japanese_for_tts",
    "normalize_dialogue_prosody",
    "resolve_tts_options",
    "SLICING_METHODS",
    "TTS_PRESETS",
    "_TTS_NUMERIC_RANGES",
    "_TTS_STRING_MAXLEN",
    "validate_user_tts_options",
    "VoiceProfileWeightSpec",
    "_extract_weight_spec",
    "DYNAMIC_SPEED_MIN",
    "DYNAMIC_SPEED_MAX",
    "DYNAMIC_TEMP_MIN",
    "DYNAMIC_TEMP_MAX",
    "clamp_dynamic_speed",
    "clamp_dynamic_temperature",
    "probe_audio_duration_seconds",
    "async_probe_audio_duration_seconds",
    "probe_audio_spec",
    "AudioSpec",
    "AudioSpecCache",
    "_AUDIO_SPEC_CACHE",
    "_GLOBAL_AUDIO_SPEC_CACHE",
    "validate_reference_audio",
    "REFERENCE_AUDIO_MIN_SECONDS",
    "REFERENCE_AUDIO_MAX_SECONDS",
    "resolve_reference_audio_path",
    "SILENT_AUDIO_ERROR",
    "wav_peak_amplitude",
    "is_silent_audio",
    "wav_is_silent",
]


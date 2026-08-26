"""
OpenAI-compatible Speech-to-Text (STT/ASR) Adapter for galgame2voice.
Supports OpenAI Whisper, Groq Whisper, and other OpenAI-compatible audio transcription endpoints.
"""

import time
from typing import Dict, Any, Optional
import httpx

from galgame2voice.adapters.base import BaseSTTAdapter, TestResult


def _get_mime_type(filename: str) -> str:
    """Infers MIME type from audio file extension."""
    ext = filename.lower().split(".")[-1] if "." in filename else "wav"
    mime_map = {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "ogg": "audio/ogg",
        "flac": "audio/flac",
        "m4a": "audio/m4a",
        "webm": "audio/webm",
    }
    return mime_map.get(ext, "audio/wav")


class OpenAICompatibleSTTAdapter(BaseSTTAdapter):
    """
    STT Adapter utilizing OpenAI Whisper / Groq Whisper / standard multipart /audio/transcriptions.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        client_override: Optional[Any] = None,
        default_model: str = "whisper-1",
        **kwargs: Any,
    ):
        super().__init__(api_key=api_key, base_url=base_url, **kwargs)
        self.mock_server = client_override
        self.default_model = default_model

    def _validate_credentials(self) -> None:
        if not self.api_key:
            raise ValueError("Authentication error: Invalid API key")

    async def transcribe(
        self,
        audio_bytes: bytes,
        filename: str = "audio.wav",
        language: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """
        Transcribes binary audio payload into plain text via multipart POST.
        """
        if not audio_bytes:
            raise ValueError("Audio bytes cannot be empty")

        self._validate_credentials()

        # Handle mock server for test environments
        if self.mock_server:
            resp = await self.mock_server.handle_transcription(
                files={"file": (filename, audio_bytes)},
                data={"language": language, **kwargs},
            )
            if resp.status_code in (401, 403):
                raise ValueError("Authentication error")
            if resp.status_code != 200:
                raise RuntimeError(f"STT API error {resp.status_code}: {resp.text}")
            return resp.json().get("text", "")

        url = f"{self.base_url}/audio/transcriptions"
        model = kwargs.get("model") or self.default_model

        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }
        custom_headers = self.extra_config.get("custom_headers")
        if isinstance(custom_headers, dict):
            headers.update(custom_headers)

        data_fields: Dict[str, Any] = {"model": model}
        if language:
            data_fields["language"] = language

        for k, v in kwargs.items():
            if k not in ("model", "language", "client_override", "custom_headers", "timeout_s"):
                data_fields[k] = v

        mime_type = _get_mime_type(filename)
        files = {
            "file": (filename, audio_bytes, mime_type)
        }

        timeout_s = float(self.extra_config.get("timeout_s", 60.0))

        async with httpx.AsyncClient(timeout=timeout_s) as client:
            try:
                resp = await client.post(url, headers=headers, data=data_fields, files=files)
            except httpx.RequestError as exc:
                raise RuntimeError(f"Network error calling STT service at {url}: {exc}") from exc

            if resp.status_code in (401, 403):
                raise ValueError(f"Authentication error ({resp.status_code}): {resp.text}")
            if resp.status_code != 200:
                raise RuntimeError(f"STT API returned status {resp.status_code}: {resp.text}")

            try:
                result_json = resp.json()
                return result_json.get("text", "")
            except Exception as exc:
                raise RuntimeError(f"Failed to parse STT response JSON: {exc} | Body: {resp.text[:200]}") from exc

    async def test_connection(self) -> TestResult:
        """
        Tests STT connectivity and credentials.
        """
        if not self.api_key:
            return TestResult(
                success=False,
                message="STT Auth failed: Invalid API key",
                latency_ms=None,
            )

        if self.mock_server:
            return TestResult(
                success=True,
                message="STT service ready",
                latency_ms=40.0,
            )

        t0 = time.perf_counter()
        url = f"{self.base_url}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(url, headers=headers)
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                if resp.status_code in (200, 404):  # 404 on /models is fine if endpoint is specialized
                    return TestResult(
                        success=True,
                        message=f"Connected successfully to STT endpoint {self.base_url}",
                        latency_ms=latency_ms,
                    )
                if resp.status_code in (401, 403):
                    return TestResult(
                        success=False,
                        message=f"STT Auth failed ({resp.status_code})",
                        latency_ms=latency_ms,
                    )
                return TestResult(
                    success=True,
                    message="STT service reachable",
                    latency_ms=latency_ms,
                )
            except Exception as exc:
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                return TestResult(
                    success=False,
                    message=f"STT Connection error: {exc}",
                    latency_ms=latency_ms,
                )


# Alias for backward compatibility
OpenAISTTAdapter = OpenAICompatibleSTTAdapter

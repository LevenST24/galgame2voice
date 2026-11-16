"""ASGI StaticCacheControlMiddleware Cache-Control header tests for /audio/ and /static/assets/."""

from fastapi.testclient import TestClient

from galgame2voice.config import get_settings
from galgame2voice.main import create_app


class TestAudioCacheControlHeaders:
    """Verifies ASGI StaticCacheControlMiddleware preserving immutable headers on /audio/cache/."""

    def test_asgi_cache_audio_immutable_header(self, tmp_path):
        """GET /audio/cache/*.wav must return HTTP 200 with Cache-Control: public, max-age=31536000, immutable."""
        settings = get_settings()
        cache_dir = settings.audio_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        test_wav = cache_dir / "tts_immutable_sample.wav"
        test_wav.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00")

        app = create_app()
        client = TestClient(app)

        resp = client.get("/audio/cache/tts_immutable_sample.wav")
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == "public, max-age=31536000, immutable"
        assert resp.content.startswith(b"RIFF")

    def test_asgi_non_cache_audio_private_header(self):
        """GET /audio/*.wav (non-cache) must return HTTP 200 with Cache-Control: private, max-age=0."""
        settings = get_settings()
        settings.audio_dir.mkdir(parents=True, exist_ok=True)
        user_audio = settings.audio_dir / "user_mic_ephemeral.wav"
        user_audio.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00")

        app = create_app()
        client = TestClient(app)

        resp = client.get("/audio/user_mic_ephemeral.wav")
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == "private, max-age=0"

    def test_asgi_cache_control_error_responses_never_immutable(self):
        """404 error responses under /audio/cache/ must never receive immutable cache headers."""
        app = create_app()
        client = TestClient(app)

        resp = client.get("/audio/cache/missing_audio_segment_404.wav")
        assert resp.status_code == 404
        assert resp.headers.get("Cache-Control") == "no-cache, no-store, must-revalidate"

    def test_asgi_cache_control_traversal_attempts_protected(self):
        """Path traversal payloads under /audio/cache/ must be blocked or not receive immutable headers."""
        app = create_app()
        client = TestClient(app)

        resp = client.get("/audio/cache/../../secret.txt")
        assert resp.status_code in (403, 404)
        assert resp.headers.get("Cache-Control") != "public, max-age=31536000, immutable"

    _CACHE_SAMPLE = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00"
    _IMMUTABLE = "public, max-age=31536000, immutable"
    _PRIVATE = "private, max-age=0"
    _NO_STORE = "no-cache, no-store, must-revalidate"

    def _audio_client(self, *relative_paths: str) -> TestClient:
        settings = get_settings()
        for relative in relative_paths:
            path = settings.audio_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self._CACHE_SAMPLE)
        return TestClient(create_app())

    def test_asgi_cache_audio_head_is_immutable(self):
        """HEAD /audio/cache/*.wav stays immutable. A GET 200 check does not observe HEAD."""
        client = self._audio_client("cache/tts_sample_valid.wav")
        head_resp = client.head("/audio/cache/tts_sample_valid.wav")
        assert head_resp.status_code == 200
        assert head_resp.headers.get("Cache-Control") == self._IMMUTABLE

    def test_asgi_cache_audio_partial_content_is_immutable(self):
        """206 under /audio/cache/ keeps the immutable header and a Content-Range."""
        client = self._audio_client("cache/tts_sample_valid.wav")
        range_resp = client.get(
            "/audio/cache/tts_sample_valid.wav", headers={"Range": "bytes=0-3"}
        )
        assert range_resp.status_code == 206
        assert range_resp.headers.get("Cache-Control") == self._IMMUTABLE
        assert "bytes 0-3/" in range_resp.headers.get("Content-Range", "")

    def test_asgi_cache_audio_unsatisfiable_range_is_not_stored(self):
        """416 is not a 404. It must be no-store, not the immutable cache value."""
        client = self._audio_client("cache/tts_sample_valid.wav")
        resp_416 = client.get(
            "/audio/cache/tts_sample_valid.wav", headers={"Range": "bytes=9999-9999"}
        )
        assert resp_416.status_code == 416
        assert resp_416.headers.get("Cache-Control") == self._NO_STORE

    def test_asgi_cache_audio_post_is_not_immutable(self):
        """POST is outside the GET/HEAD branch. A 404 no-store check does not cover 405."""
        client = self._audio_client("cache/tts_sample_valid.wav")
        resp_405 = client.post("/audio/cache/tts_sample_valid.wav")
        assert resp_405.status_code == 405
        assert "immutable" not in resp_405.headers.get("Cache-Control", "")

    def test_asgi_misleading_cache_directory_is_private(self):
        """cache_not_really shares a prefix with cache but is not the cache directory."""
        client = self._audio_client("cache_not_really/misleading_cache.wav")
        resp_fake = client.get("/audio/cache_not_really/misleading_cache.wav")
        assert resp_fake.status_code == 200
        assert resp_fake.headers.get("Cache-Control") == self._PRIVATE

    def test_asgi_cache_uppercase_wav_extension_is_immutable(self):
        """An uppercase .WAV name under /audio/cache/ is still immutable."""
        client = self._audio_client("cache/tts_sample_upper.WAV")
        resp_upper = client.get("/audio/cache/tts_sample_upper.WAV")
        assert resp_upper.status_code == 200
        assert resp_upper.headers.get("Cache-Control") == self._IMMUTABLE

    def test_asgi_percent_encoded_cache_segment_is_immutable(self):
        """Percent-encoded %63ache is the cache segment and stays immutable."""
        client = self._audio_client("cache/tts_sample_valid.wav")
        resp_enc = client.get("/audio/%63ache/tts_sample_valid.wav")
        assert resp_enc.status_code == 200
        assert resp_enc.headers.get("Cache-Control") == self._IMMUTABLE

    def test_asgi_cache_parent_traversal_missing_file_is_not_stored(self):
        """/audio/cache/../missing_traversal.wav is a 404 and must be no-store."""
        client = self._audio_client("cache/tts_sample_valid.wav")
        resp_inner = client.get("/audio/cache/../missing_traversal.wav")
        assert resp_inner.status_code == 404
        assert resp_inner.headers.get("Cache-Control") == self._NO_STORE

    def test_asgi_percent_encoded_cache_traversal_is_not_immutable(self):
        """Encoded .. leaving /audio/cache/ must not keep the immutable cache header."""
        client = self._audio_client("cache/tts_sample_valid.wav", "user_private_recording.wav")
        resp_pct_priv = client.get("/audio/cache/%2e%2e/user_private_recording.wav")
        assert resp_pct_priv.headers.get("Cache-Control") != self._IMMUTABLE
        if resp_pct_priv.status_code == 200:
            assert resp_pct_priv.headers.get("Cache-Control") == self._PRIVATE

    def test_asgi_static_assets_encoded_traversal_is_not_immutable(self):
        """Encoded .. leaving /static/assets/ must not receive the immutable asset header."""
        client = TestClient(create_app())
        resp_pct_html = client.get("/static/assets/%2e%2e/index.html")
        assert resp_pct_html.headers.get("Cache-Control") != self._IMMUTABLE

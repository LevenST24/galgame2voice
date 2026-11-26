"""ASGI StaticCacheControlMiddleware Cache-Control header tests for /audio/ and /static/assets/."""

from pathlib import Path
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport
import pytest

from galgame2voice.config import get_settings
from galgame2voice.main import AudioStaticFiles, create_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
        client = TestClient(app, base_url="http://127.0.0.1:8080")

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
        client = TestClient(app, base_url="http://127.0.0.1:8080")

        resp = client.get("/audio/user_mic_ephemeral.wav")
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == "private, max-age=0"

    def test_asgi_cache_control_error_responses_never_immutable(self):
        """404 error responses under /audio/cache/ must never receive immutable cache headers."""
        app = create_app()
        client = TestClient(app, base_url="http://127.0.0.1:8080")

        resp = client.get("/audio/cache/missing_audio_segment_404.wav")
        assert resp.status_code == 404
        assert resp.headers.get("Cache-Control") == "no-cache, no-store, must-revalidate"

    def test_asgi_cache_control_traversal_attempts_protected(self):
        """Path traversal payloads under /audio/cache/ must be blocked or not receive immutable headers."""
        app = create_app()
        client = TestClient(app, base_url="http://127.0.0.1:8080")

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
        return TestClient(create_app(), base_url="http://127.0.0.1:8080")

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


class TestStaticAssetCaching:
    @pytest.mark.asyncio
    async def test_static_assets_immutable_cache_header(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Request under /static/assets/ (real existing asset)
            static_assets = list((PROJECT_ROOT / "galgame2voice" / "static" / "assets").glob("index-*.js"))
            asset_name = static_assets[0].name if static_assets else "index-C1humFBM.js"
            resp = await client.get(f"/static/assets/{asset_name}")
            assert resp.status_code == 200
            assert "cache-control" in resp.headers
            assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"

            # Entry point index.html
            resp_index = await client.get("/")
            assert resp_index.status_code == 200
            assert resp_index.headers.get("cache-control") == "no-cache"

            # Legacy /settings.html redirects to unified SPA
            resp_settings = await client.get("/settings.html")
            assert resp_settings.status_code == 307
            assert resp_settings.headers["location"] == "/?settings=1"

    @pytest.mark.asyncio
    async def test_static_assets_error_status_not_cached_immutably(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/static/assets/non_existent_asset_xyz.js")
            assert resp.status_code == 404
            assert "immutable" not in resp.headers.get("cache-control", "")
            assert "no-cache" in resp.headers.get("cache-control", "")

    @pytest.mark.asyncio
    async def test_static_assets_post_method_not_cached(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            static_assets = list((PROJECT_ROOT / "galgame2voice" / "static" / "assets").glob("index-*.js"))
            asset_name = static_assets[0].name if static_assets else "index-C1humFBM.js"
            resp = await client.post(f"/static/assets/{asset_name}")
            # POST should not receive immutable static cache header
            assert "immutable" not in resp.headers.get("cache-control", "")


@pytest.mark.asyncio
async def test_audio_static_files_caching_headers(tmp_path: Path):
    """AudioStaticFiles only adds Accept-Ranges; Cache-Control belongs to the middleware."""
    audio_dir = tmp_path / "audio"
    cache_dir = audio_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    cache_file = cache_dir / "abc12345.wav"
    cache_file.write_bytes(b"RIFFcache")

    ephemeral_file = audio_dir / "voice_001.wav"
    ephemeral_file.write_bytes(b"RIFFephemeral")

    static_handler = AudioStaticFiles(directory=str(audio_dir))

    # Test cache/ URL
    scope_cache = {
        "type": "http",
        "method": "GET",
        "path": "/cache/abc12345.wav",
        "headers": [],
    }
    resp_cache = await static_handler.get_response("cache/abc12345.wav", scope_cache)
    assert resp_cache.status_code == 200
    assert resp_cache.headers.get("cache-control") is None
    assert resp_cache.headers.get("accept-ranges") == "bytes"

    # Test regular ephemeral audio URL
    scope_eph = {
        "type": "http",
        "method": "GET",
        "path": "/voice_001.wav",
        "headers": [],
    }
    resp_eph = await static_handler.get_response("voice_001.wav", scope_eph)
    assert resp_eph.status_code == 200
    assert resp_eph.headers.get("cache-control") is None
    assert resp_eph.headers.get("accept-ranges") == "bytes"

"""Credential cipher, persistent-key failure and legacy migration contracts."""

import base64
import builtins
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import importlib.util
import os
from pathlib import Path
import threading

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from galgame2voice.config import get_settings
from galgame2voice.database.session import get_db, init_db
from galgame2voice.security import crypto


@pytest.fixture
def fallback(monkeypatch):
    monkeypatch.setattr(crypto, "_HAS_DPAPI", False)
    monkeypatch.delenv("GALGAME2VOICE_SECRET_KEY", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_MASTER_KEY", raising=False)
    path = get_settings().data_dir / ".master_key"
    return path


def test_new_cipher_is_aes_gcm_with_unique_nonces(fallback):
    encrypted = [crypto.encrypt_secret("test credential") for _ in range(2)]
    assert encrypted[0] != encrypted[1]
    key = fallback.read_bytes()
    assert len(key) == 32
    for value in encrypted:
        payload = base64.b64decode(value[4:])
        assert payload[0] == 1
        assert AESGCM(key).decrypt(payload[1:13], payload[13:], None) == b"test credential"
        changed = payload[:-1] + bytes([payload[-1] ^ 1])
        with pytest.raises(InvalidTag):
            AESGCM(key).decrypt(changed[1:13], changed[13:], None)
        assert crypto.decrypt_secret(value) == "test credential"


@pytest.mark.parametrize("size", [0, 31, 33])
def test_corrupt_master_key_is_not_replaced(fallback, size):
    original = b"X" * size
    fallback.write_bytes(original)
    for _ in range(2):
        with pytest.raises(crypto.MasterKeyError, match="expected 32 bytes"):
            crypto.encrypt_secret("test credential")
        assert fallback.read_bytes() == original


def test_unwritable_key_storage_aborts_encryption(fallback, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("read-only mount")
    monkeypatch.setattr(crypto.tempfile, "mkstemp", denied)
    with pytest.raises(crypto.MasterKeyError, match="Cannot persist"):
        crypto.encrypt_secret("test credential")
    assert not fallback.exists()


def test_missing_decryption_key_does_not_generate_a_replacement(fallback):
    value = crypto.encrypt_secret("test credential")
    original_key = fallback.read_bytes()
    fallback.unlink()
    with pytest.raises(crypto.MasterKeyError, match="missing"):
        crypto.decrypt_secret(value)
    assert not fallback.exists()
    fallback.write_bytes(original_key)
    assert crypto.decrypt_secret(value) == "test credential"


def test_unreadable_key_is_reported_without_overwrite(fallback, monkeypatch):
    fallback.write_bytes(b"X" * 32)
    original_read = Path.read_bytes
    def denied(path):
        if path == fallback:
            raise PermissionError("denied")
        return original_read(path)
    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(crypto.MasterKeyError, match="Cannot read"):
        crypto.encrypt_secret("test credential")
    assert original_read(fallback) == b"X" * 32


def test_concurrent_first_writes_publish_one_complete_key(fallback):
    barrier = threading.Barrier(8)
    def encrypt(index):
        barrier.wait(timeout=5)
        return crypto.encrypt_secret(f"test credential {index}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(encrypt, range(8)))
    assert len(fallback.read_bytes()) == 32
    assert [crypto.decrypt_secret(value) for value in values] == [f"test credential {i}" for i in range(8)]
    assert list(fallback.parent.glob(".master_key.*")) == []
    if os.name != "nt":
        assert fallback.stat().st_mode & 0o777 == 0o600


def test_missing_cryptography_fails_module_loading(monkeypatch):
    real_import = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == "cryptography.hazmat.primitives.ciphers.aead":
            raise ModuleNotFoundError("cryptography absent")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", unavailable)
    spec = importlib.util.spec_from_file_location("isolated_crypto_dependency_probe", crypto.__file__)
    with pytest.raises(ModuleNotFoundError, match="cryptography absent"):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


def legacy_secret(key, text):
    # A fixed fixture for the removed writer, kept solely to exercise migration.
    nonce = b"N" * 16
    plaintext = text.encode()
    block = hmac.new(key, nonce + (1).to_bytes(4, "big"), hashlib.sha256).digest()
    ciphertext = bytes(p ^ k for p, k in zip(plaintext, block[:len(plaintext)], strict=True))
    tag = hmac.new(key, b"auth" + nonce + ciphertext, hashlib.sha256).digest()
    return "enc:" + base64.b64encode(b"\x02" + nonce + tag + ciphertext).decode()


async def test_existing_legacy_credentials_migrate_without_data_loss(fallback, tmp_path):
    path = tmp_path / "migration.db"
    await init_db(path)
    legacy = legacy_secret(fallback.read_bytes(), "test-api-key")
    async with get_db(path) as conn:
        await conn.execute("UPDATE providers SET api_key = ? WHERE id = 'deepseek'", (legacy,))
        await conn.commit()
    await init_db(path)
    async with get_db(path) as conn:
        stored = (await (await conn.execute("SELECT api_key FROM providers WHERE id = 'deepseek'")).fetchone())[0]
    assert base64.b64decode(stored[4:])[0] == 1
    assert crypto.decrypt_secret(stored) == "test-api-key"


async def test_corrupt_legacy_record_aborts_migration_without_overwrite(fallback, tmp_path):
    path = tmp_path / "migration.db"
    await init_db(path)
    legacy = legacy_secret(fallback.read_bytes(), "test-api-key")
    payload = bytearray(base64.b64decode(legacy[4:]))
    payload[-1] ^= 1
    damaged = "enc:" + base64.b64encode(payload).decode()
    async with get_db(path) as conn:
        await conn.execute("UPDATE providers SET api_key = ? WHERE id = 'deepseek'", (damaged,))
        await conn.commit()
    with pytest.raises(ValueError, match="Integrity"):
        await init_db(path)
    async with get_db(path) as conn:
        assert (await (await conn.execute("SELECT api_key FROM providers WHERE id = 'deepseek'")).fetchone())[0] == damaged

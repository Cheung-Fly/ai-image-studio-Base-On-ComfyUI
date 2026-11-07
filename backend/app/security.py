"""Authenticated encryption for API keys stored in the application database."""

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

_TOKEN_PREFIX = "v1:"
_INSECURE_DEFAULT_KEY = "change-me-in-production-encryption-key"
_MIN_KEY_LENGTH = 32


class SecretConfigurationError(ValueError):
    """Raised when secret encryption is requested without a safe key."""


def _configured_key() -> str:
    key = settings.encryption_key.strip()
    if (
        len(key) < _MIN_KEY_LENGTH
        or key == _INSECURE_DEFAULT_KEY
    ):
        raise SecretConfigurationError(
            "请设置至少 32 个字符的随机 ENCRYPTION_KEY；不要使用示例默认值。"
        )
    return key


def _fernet(key: str) -> Fernet:
    derived_key = hashlib.sha256(key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(derived_key))


def _derive_legacy_keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(
            hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        )
        counter += 1
    return bytes(output[:length])


def _legacy_keys() -> list[str]:
    keys = []
    for candidate in (settings.legacy_encryption_key, settings.encryption_key):
        key = candidate.strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def _versioned_keys() -> list[str]:
    keys = []
    for candidate in (settings.encryption_key, settings.legacy_encryption_key):
        key = candidate.strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def is_encrypted_with_current_key(token: str) -> bool:
    """Return whether a versioned token authenticates with the active key."""
    if not token.startswith(_TOKEN_PREFIX) or not settings.encryption_key.strip():
        return False
    try:
        _fernet(settings.encryption_key.strip()).decrypt(
            token[len(_TOKEN_PREFIX) :].encode("ascii")
        )
    except (InvalidToken, UnicodeEncodeError):
        return False
    return True


def _decrypt_legacy_secret(token: str) -> str:
    # Read-only compatibility for credentials written before authenticated encryption.
    try:
        raw = base64.b64decode(token.encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("密文格式非法") from exc
    if len(raw) < 17:
        raise ValueError("密文长度非法")

    nonce, ciphertext = raw[:16], raw[16:]
    for key in _legacy_keys():
        keystream = _derive_legacy_keystream(key.encode("utf-8"), nonce, len(ciphertext))
        plaintext = bytes(a ^ b for a, b in zip(ciphertext, keystream, strict=True))
        try:
            decoded = plaintext.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if decoded and decoded.isprintable():
            return decoded
    raise ValueError(
        "旧版 API Key 无法解密；请检查 ENCRYPTION_KEY 或迁移用的 LEGACY_ENCRYPTION_KEY。"
    )


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret with authenticated Fernet encryption."""
    encrypted = _fernet(_configured_key()).encrypt(plaintext.encode("utf-8"))
    return _TOKEN_PREFIX + encrypted.decode("ascii")


def decrypt_secret(token: str) -> str:
    """Decrypt a versioned secret, retaining read support for legacy ciphertexts."""
    if not token.startswith(_TOKEN_PREFIX):
        return _decrypt_legacy_secret(token)

    encrypted = token[len(_TOKEN_PREFIX) :]
    keys = _versioned_keys()
    if not keys:
        raise SecretConfigurationError("请设置 ENCRYPTION_KEY 才能解密已保存的 API Key。")
    for key in keys:
        try:
            return _fernet(key).decrypt(encrypted.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError):
            continue
    raise ValueError(
        "API Key 解密或完整性校验失败；请检查 ENCRYPTION_KEY 和 LEGACY_ENCRYPTION_KEY。"
    )

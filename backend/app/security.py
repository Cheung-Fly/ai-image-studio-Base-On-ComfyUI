"""自定义模型 API Key 的加密存储工具。"""

import base64
import hashlib
import hmac
import secrets

from .config import settings


def _derive_keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:length]


def encrypt_secret(plaintext: str) -> str:
    """用 settings.encryption_key 加密字符串，返回 base64(nonce + 密文)。"""
    key = settings.encryption_key.encode("utf-8")
    nonce = secrets.token_bytes(16)
    pt = plaintext.encode("utf-8")
    keystream = _derive_keystream(key, nonce, len(pt))
    ct = bytes(a ^ b for a, b in zip(pt, keystream))
    return base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt_secret(token: str) -> str:
    """解密 encrypt_secret 的输出；失败抛 ValueError。"""
    raw = base64.urlsafe_b64decode(token)
    if len(raw) < 17:
        raise ValueError("密文长度非法")
    nonce, ct = raw[:16], raw[16:]
    key = settings.encryption_key.encode("utf-8")
    keystream = _derive_keystream(key, nonce, len(ct))
    pt = bytes(a ^ b for a, b in zip(ct, keystream))
    return pt.decode("utf-8")

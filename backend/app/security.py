"""安全工具：密码哈希（PBKDF2）+ JWT（HS256）签发与校验。

全部基于 Python 标准库实现，避免额外依赖。
- 密码用 PBKDF2-HMAC-SHA256 + 随机盐 + 20 万次迭代（OWASP 推荐量级）。
- JWT 用 HS256（HMAC-SHA256），payload 含 sub（用户 id）与 exp（过期时间）。
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from .config import settings

_PBKDF2_ITERATIONS = 200_000


# ---------- 密码哈希 ----------

def hash_password(password: str) -> str:
    """返回 "pbkdf2_sha256$iterations$salt$hash" 格式的哈希串。"""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码是否匹配存储的哈希串。"""
    try:
        algo, iterations, salt, expected = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
        )
        return hmac.compare_digest(dk.hex(), expected)
    except (ValueError, AttributeError):
        return False


# ---------- JWT ----------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_token(user_id: int) -> str:
    """签发一个 HS256 JWT，exp 为当前时间 + 有效期。"""
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user_id),
        "iat": int(time.time()),
        "exp": int(time.time()) + settings.jwt_expire_minutes * 60,
    }
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}"
    signature = hmac.new(
        settings.jwt_secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64url_encode(signature)}"


def decode_token(token: str) -> dict[str, Any] | None:
    """校验 JWT 并返回 payload；无效/过期返回 None。"""
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        signing_input = f"{header_b64}.{payload_b64}"
        expected = hmac.new(
            settings.jwt_secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256
        ).digest()
        actual = _b64url_decode(signature_b64)
        if not hmac.compare_digest(expected, actual):
            return None
        payload = json.loads(_b64url_decode(payload_b64))
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


# ---------- 对称加密（加密存储 API Key 用） ----------

def _derive_keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """用 HMAC-SHA256 作为伪随机函数（PRF），生成指定长度的密钥流（类 CTR 模式）。"""
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

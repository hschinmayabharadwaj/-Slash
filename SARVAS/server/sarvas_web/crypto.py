"""Token encryption at rest (AES-256-GCM) with a key kept in a 0600 file, or in
WEB_ENCRYPTION_KEY (base64, 32 bytes) where the disk isn't persistent."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def load_or_create_key(data_dir: str) -> bytes:
    if env := os.environ.get("WEB_ENCRYPTION_KEY"):
        key = base64.b64decode(env)
        if len(key) != 32:
            raise ValueError("WEB_ENCRYPTION_KEY must be 32 bytes, base64-encoded")
        return key
    path = Path(data_dir) / "web_secret.key"
    if path.exists():
        return base64.b64decode(path.read_text().strip())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = os.urandom(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.b64encode(key).decode())
    return key


def encrypt(key: bytes, obj) -> str:
    """iv (12) | tag (16) | ciphertext, base64."""
    iv = os.urandom(12)
    sealed = AESGCM(key).encrypt(iv, json.dumps(obj).encode(), None)
    return base64.b64encode(iv + sealed[-16:] + sealed[:-16]).decode()


def decrypt(key: bytes, text: str):
    try:
        buf = base64.b64decode(text)
        return json.loads(AESGCM(key).decrypt(buf[:12], buf[28:] + buf[12:28], None))
    except Exception:
        return None


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def safe_equal(a, b) -> bool:
    return hmac.compare_digest(str(a or "").encode(), str(b or "").encode())

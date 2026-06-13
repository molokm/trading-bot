import asyncio
import base64
import hashlib
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from cryptography.fernet import Fernet

PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

tokens: dict[str, dict] = {}
TOKEN_TTL = timedelta(hours=24)

# ── Rate limiting ──
_attempts: dict[str, list[float]] = defaultdict(list)
MAX_ATTEMPTS = 3
WINDOW_SEC = 60
DELAY_SEC = 1.0

def check_rate_limit(ip: str) -> Optional[float]:
    now = time.time()
    _attempts[ip] = [t for t in _attempts[ip] if now - t < WINDOW_SEC]
    if len(_attempts[ip]) >= MAX_ATTEMPTS:
        return DELAY_SEC
    return None

def record_attempt(ip: str, success: bool):
    if success:
        _attempts.pop(ip, None)
    else:
        _attempts[ip].append(time.time())

# ── Token encryption ──
def _get_fernet() -> Fernet:
    key_b64 = os.getenv("TOKEN_ENCRYPTION_KEY", "")
    if key_b64:
        return Fernet(key_b64.encode() if not key_b64.endswith("=") else key_b64)
    password = PASSWORD or "insecure-default-key-for-dev"
    key_bytes = hashlib.sha256(password.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key_bytes))

def _encrypt(raw: str) -> str:
    return _get_fernet().encrypt(raw.encode()).decode()

def _decrypt(token: str) -> Optional[str]:
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except Exception:
        return None

# ── Session management ──
def _new_token(role: str) -> str:
    raw = f"{role}_{uuid.uuid4().hex}"
    tokens[raw] = {"role": role, "created": datetime.now()}
    return _encrypt(raw)

def _expired(token_data: dict) -> bool:
    return datetime.now() - token_data["created"] > TOKEN_TTL

def login(password: str) -> Optional[str]:
    if not PASSWORD:
        return _new_token("admin")
    if password == PASSWORD:
        return _new_token("admin")
    return None

def guest() -> str:
    return _new_token("guest")

def _resolve(token_enc: str) -> Optional[str]:
    return _decrypt(token_enc)

def validate(token_enc: str) -> Optional[str]:
    raw = _resolve(token_enc)
    if raw is None or raw not in tokens:
        return None
    data = tokens[raw]
    if _expired(data):
        del tokens[raw]
        return None
    return data["role"]

def logout(token_enc: str):
    raw = _resolve(token_enc)
    if raw:
        tokens.pop(raw, None)

def is_admin(token_enc: str) -> bool:
    return validate(token_enc) == "admin"

def is_authenticated(token_enc: str) -> bool:
    return validate(token_enc) is not None

def cleanup():
    now = datetime.now()
    expired = [k for k, v in tokens.items() if now - v["created"] > TOKEN_TTL]
    for k in expired:
        del tokens[k]

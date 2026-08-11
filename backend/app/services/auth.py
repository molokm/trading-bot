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
_guest_attempts: dict[str, list[float]] = defaultdict(list)
MAX_ATTEMPTS = 3
WINDOW_SEC = 60
GUEST_MAX_PER_MIN = 10

def check_rate_limit(ip: str) -> bool:
    """True if this IP has too many recent failed login attempts."""
    now = time.time()
    _attempts[ip] = [t for t in _attempts[ip] if now - t < WINDOW_SEC]
    if len(_attempts) > 10000:
        _attempts.clear()
    return len(_attempts[ip]) >= MAX_ATTEMPTS

def record_attempt(ip: str, success: bool):
    if success:
        _attempts.pop(ip, None)
    else:
        _attempts[ip].append(time.time())

def guest_rate_limited(ip: str) -> bool:
    """True if this IP is minting guest tokens too fast (memory DoS guard)."""
    now = time.time()
    _guest_attempts[ip] = [t for t in _guest_attempts[ip] if now - t < WINDOW_SEC]
    if len(_guest_attempts) > 10000:
        _guest_attempts.clear()
    return len(_guest_attempts[ip]) >= GUEST_MAX_PER_MIN

def record_guest(ip: str):
    _guest_attempts[ip].append(time.time())

# ── Token encryption ──
def _get_fernet() -> Fernet:
    key_b64 = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
    if key_b64:
        try:
            return Fernet(key_b64.encode())
        except Exception:
            pass
    # Fallback: per-process random key (tokens are in-memory only, so a
    # restart invalidates sessions regardless of the key).
    if not hasattr(_get_fernet, "_fallback_key"):
        _get_fernet._fallback_key = Fernet.generate_key()
    return Fernet(_get_fernet._fallback_key)

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

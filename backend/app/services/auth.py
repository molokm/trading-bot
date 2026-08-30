"""Auth sessions: signed JWT so logins survive process restart / Render sleep.

Tokens are self-contained (role, user_id, exp). No in-memory session store is
required for validation. Optional jti blacklist is kept only for logout within
the current process lifetime.
"""
from __future__ import annotations

import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.fernet import Fernet
from jose import JWTError, jwt

PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

TOKEN_TTL = timedelta(hours=24)
JWT_ALG = "HS256"

# In-process logout blacklist (jti -> exp unix). Best-effort only.
_blacklist: dict[str, float] = {}

# ── Rate limiting (in-memory; resets on restart — acceptable) ──
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


# ── Encryption for secrets at rest (OKX keys, etc.) ──
def _require_stable_secrets() -> None:
    """Fail-fast in production when encryption key is missing."""
    strict = (os.getenv("REQUIRE_ENCRYPTION_KEY") or "").strip().lower() in ("1", "true", "yes")
    if not strict:
        if os.getenv("DATABASE_URL", "").startswith(("postgres://", "postgresql://")):
            strict = True
        if os.getenv("DASHBOARD_PASSWORD", "").strip():
            strict = True
    if not strict:
        return
    key_b64 = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
    if not key_b64:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY is required. Generate with: "
            "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )
    try:
        Fernet(key_b64.encode())
    except Exception as e:
        raise RuntimeError(f"TOKEN_ENCRYPTION_KEY is invalid: {e}") from e


def _get_fernet() -> Fernet:
    key_b64 = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
    if key_b64:
        try:
            return Fernet(key_b64.encode())
        except Exception as e:
            raise RuntimeError(f"TOKEN_ENCRYPTION_KEY is invalid: {e}") from e
    if not hasattr(_get_fernet, "_fallback_key"):
        _get_fernet._fallback_key = Fernet.generate_key()
        print(
            "[auth] WARNING: TOKEN_ENCRYPTION_KEY unset — ephemeral Fernet key "
            "(OKX secrets will not survive restart)",
            flush=True,
        )
    return Fernet(_get_fernet._fallback_key)


def _encrypt(raw: str) -> str:
    return _get_fernet().encrypt(raw.encode()).decode()


def _decrypt(token: str) -> Optional[str]:
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except Exception:
        return None


def encrypt_str(raw: str) -> str:
    """Encrypt a secret for DB storage (e.g. per-user OKX keys)."""
    if not raw:
        return ""
    return _encrypt(raw)


def decrypt_str(cipher: str) -> str:
    """Decrypt a secret from DB storage. Returns '' on failure."""
    if not cipher:
        return ""
    return _decrypt(cipher) or ""


# ── JWT secret (must be stable across restarts on Render) ──
def _jwt_secret() -> str:
    for env_key in ("JWT_SECRET", "TOKEN_ENCRYPTION_KEY", "DASHBOARD_PASSWORD"):
        val = os.getenv(env_key, "").strip()
        if val:
            return val
    # Last resort: process-local (sessions die on restart — same as old behaviour)
    if not hasattr(_jwt_secret, "_fallback"):
        _jwt_secret._fallback = uuid.uuid4().hex + uuid.uuid4().hex
    return _jwt_secret._fallback


def _new_token(role: str, user_id: str = None) -> str:
    now = datetime.now(timezone.utc)
    jti = uuid.uuid4().hex
    payload = {
        "role": role,
        "uid": user_id,
        "jti": jti,
        "iat": now,
        "exp": now + TOKEN_TTL,
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALG)


def _decode(token: str) -> Optional[dict]:
    if not token:
        return None
    try:
        data = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALG])
    except JWTError:
        return None
    jti = data.get("jti")
    if jti and jti in _blacklist:
        if time.time() < _blacklist[jti]:
            return None
        _blacklist.pop(jti, None)
    return data


def login(password: str) -> Optional[str]:
    if not PASSWORD:
        return _new_token("admin")
    if password == PASSWORD:
        return _new_token("admin")
    return None


def guest() -> str:
    return _new_token("guest")


def grant_admin() -> str:
    """Mint an admin session token (owner, no user binding)."""
    return _new_token("admin")


def grant_user(user_id) -> str:
    """Mint a session token bound to a Telegram user account (multi-tenant)."""
    return _new_token("user", str(user_id))


def validate(token_enc: str) -> Optional[str]:
    data = _decode(token_enc)
    if not data:
        return None
    role = data.get("role")
    return role if role in ("admin", "guest", "user") else None


def get_user_id(token_enc: str) -> Optional[str]:
    """Return the Telegram user_id bound to the token, or None (owner/guest)."""
    data = _decode(token_enc)
    if not data:
        return None
    uid = data.get("uid")
    return str(uid) if uid else None


def is_admin(token_enc: str) -> bool:
    return validate(token_enc) == "admin"


def is_authenticated(token_enc: str) -> bool:
    return validate(token_enc) is not None


def logout(token_enc: str):
    data = _decode(token_enc)
    if not data:
        return
    jti = data.get("jti")
    exp = data.get("exp")
    if not jti:
        return
    # exp may be int (unix) from jose
    if isinstance(exp, (int, float)):
        exp_ts = float(exp)
    else:
        exp_ts = time.time() + TOKEN_TTL.total_seconds()
    _blacklist[jti] = exp_ts
    # prune
    now = time.time()
    dead = [k for k, v in _blacklist.items() if v < now]
    for k in dead:
        del _blacklist[k]


def cleanup():
    """Prune logout blacklist. Kept for API compatibility with old callers."""
    now = time.time()
    dead = [k for k, v in _blacklist.items() if v < now]
    for k in dead:
        del _blacklist[k]


def ensure_auth_secrets() -> None:
    _require_stable_secrets()

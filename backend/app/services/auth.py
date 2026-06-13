import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

tokens: dict[str, dict] = {}
TOKEN_TTL = timedelta(hours=24)

def _new_token(role: str) -> str:
    raw = f"{role}_{uuid.uuid4().hex}"
    tokens[raw] = {"role": role, "created": datetime.now()}
    return raw

def _expired(token_data: dict) -> bool:
    return datetime.now() - token_data["created"] > TOKEN_TTL

def login(password: str) -> Optional[str]:
    if not PASSWORD:
        return _new_token("admin")
    if password == PASSWORD:
        return _new_token("admin")
    print(f"[auth] login mismatch: input len={len(password)}, stored len={len(PASSWORD)}")
    return None

def guest() -> str:
    return _new_token("guest")

def validate(token: str) -> Optional[str]:
    if token not in tokens:
        return None
    data = tokens[token]
    if _expired(data):
        del tokens[token]
        return None
    return data["role"]

def logout(token: str):
    tokens.pop(token, None)

def is_admin(token: str) -> bool:
    return validate(token) == "admin"

def is_authenticated(token: str) -> bool:
    return validate(token) is not None

def cleanup():
    now = datetime.now()
    expired = [k for k, v in tokens.items() if now - v["created"] > TOKEN_TTL]
    for k in expired:
        del tokens[k]

"""Deterministic environment for the test suite.

Must run before anything imports app.* — several modules (database, auth) read
env at import time. Keeps tests hermetic: local SQLite in a temp dir, fixed JWT
secret, no bot auto-start.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="copix-tests-")

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("DASHBOARD_PASSWORD", "test-password")
os.environ.setdefault("OKX_DEMO", "true")
os.environ.setdefault("DATA_DIR", _TMP)
os.environ.setdefault("DATABASE_URL", f"file:{_TMP}/trading.db")
os.environ.setdefault("BOTS_AUTO_START", "0")

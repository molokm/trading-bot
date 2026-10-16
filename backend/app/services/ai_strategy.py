"""Backwards-compatibility shim — the implementation moved to app.bots.ai_discretionary.strategy.

All imports of app.services.ai_strategy are redirected here so existing code
(risk_guard.py, tests/test_mirror_strict.py, etc.) keeps working unchanged.
"""
from app.bots.ai_discretionary.strategy import *  # noqa: F401,F403
from app.bots.ai_discretionary.strategy import (  # noqa: F401
    AIStrategy,
    AIConfig,
    AIPosition,
    AI_BOT_ID,
)

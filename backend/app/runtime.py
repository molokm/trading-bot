"""Shared runtime handles for routers (stage 5).

main.py assigns these during startup; routers read them without circular imports
of main.
"""
from __future__ import annotations
from typing import Any, Optional

# Assigned in main.startup / module init
db: Any = None
client_manager: Any = None
live_manager: Any = None
showcase_manager: Any = None
telegram: Any = None
ai_bot: Any = None
strategy_mgr: Any = None

# Trading mode flags (mirrors main module vars)
env_demo: bool = True
bots_auto_start: bool = True
ai_auto: bool = True

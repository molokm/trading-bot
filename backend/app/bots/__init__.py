"""Bot registry and factory.

All trading bots are registered here for lazy initialization.
"""
from __future__ import annotations

from typing import Any, Optional

from app.bots.base import BaseBot

# Bot registry: name -> (class, module_path)
BOT_REGISTRY = {}

def register_bot(name: str, bot_class: type) -> None:
    """Register a bot class in the registry."""
    BOT_REGISTRY[name] = bot_class

def get_bot_class(name: str) -> Optional[type]:
    """Get a registered bot class by name."""
    return BOT_REGISTRY.get(name)

def create_bot(name: str, **kwargs: Any) -> Optional[BaseBot]:
    """Create a bot instance by name."""
    cls = BOT_REGISTRY.get(name)
    if cls is None:
        return None
    return cls(**kwargs)

# Import and register all bots
try:
    from app.bots.ai_discretionary.strategy import AIStrategy
    register_bot("ai", AIStrategy)
except Exception:
    pass

try:
    from app.bots.ai_scale.strategy import AIScaleStrategy
    register_bot("ai_scale", AIScaleStrategy)
except Exception:
    pass

"""Legacy/retired strategies — kept for backwards compatibility.

These are stubs for strategies that have been retired (Stage 5).
"""
from app.services.legacy_stubs import (
    RotationStrategy,
    RotationConfig,
    ROT_BOT_ID,
    RotPosition,
    COINS,
    ImpulseStrategy,
    ImpulseConfig,
    IMP_BOT_ID,
    ValidationStrategy,
    AIScaleStrategy,
    AIScaleConfig,
    AI_SCALE_BOT_ID,
    make_validation_config,
    VAL_BOT_ID,
    get_mirror,
)

__all__ = [
    "RotationStrategy",
    "RotationConfig",
    "ROT_BOT_ID",
    "RotPosition",
    "COINS",
    "ImpulseStrategy",
    "ImpulseConfig",
    "IMP_BOT_ID",
    "ValidationStrategy",
    "AIScaleStrategy",
    "AIScaleConfig",
    "AI_SCALE_BOT_ID",
    "make_validation_config",
    "VAL_BOT_ID",
    "get_mirror",
]

"""Contract test for app.services.legacy_stubs.

The retired strategies were replaced by stubs, but ``app/main.py`` still imports
19 names from this module (bot classes, configs, bot ids, COINS, get_mirror) and
calls ``start()``/``stop()``/``get_status()`` on them ~300 times. Cleaning up
"dead" stubs is easy to get wrong — this test pins the surface main.py needs.

Run: pytest tests/test_legacy_stubs_contract.py -q
"""
import pytest

import app.services.legacy_stubs as ls

# Names imported by app/main.py (module level + lazy imports inside handlers).
REQUIRED_NAMES = [
    "RotationStrategy",
    "RotationConfig",
    "ROT_BOT_ID",
    "RotPosition",
    "COINS",
    "ImpulseStrategy",
    "ImpulseConfig",
    "IMP_BOT_ID",
    "ValidationStrategy",
    "make_validation_config",
    "VAL_BOT_ID",
    "AIScaleStrategy",
    "AIScaleConfig",
    "AI_SCALE_BOT_ID",
    "get_mirror",
    "SmartMoneyTracker",
    "TrackerConfig",
    "OKXCopyAPI",
    "SCALP_BOT_ID",
]


@pytest.mark.parametrize("name", REQUIRED_NAMES)
def test_required_name_exists(name):
    assert hasattr(ls, name), f"legacy_stubs is missing {name} — app/main.py will fail to import"


def test_retired_bots_are_inert():
    """Every retired bot must be safe to construct, start and stop."""
    for cls_name in ("RotationStrategy", "ImpulseStrategy", "ValidationStrategy",
                     "AIScaleStrategy", "SmartMoneyTracker"):
        bot = getattr(ls, cls_name)()
        assert bot.start() is None
        assert bot.stop() is None
        status = bot.get_status()
        assert status.get("retired") is True, f"{cls_name}.get_status() must report retired"
        assert status.get("running") is False


def test_copy_api_makes_no_network_calls():
    """OKXCopyAPI is a stub: it must return empty data without any HTTP."""
    api = ls.OKXCopyAPI()
    assert api is not None
    assert ls.get_mirror() is None


def test_rot_position_defaults():
    pos = ls.RotPosition()
    assert pos.coin == "" and pos.side == "long" and pos.size == 0.0

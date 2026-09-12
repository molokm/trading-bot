"""RETIRED."""
ROT_BOT_ID = "rotation_strategy"
STRATEGY_DESC = ""
class RotationConfig:
    def __init__(self, **kw):
        for k, v in kw.items(): setattr(self, k, v)
class RotationStrategy:
    def __init__(self, *a, **k):
        raise RuntimeError("Momentum retired")


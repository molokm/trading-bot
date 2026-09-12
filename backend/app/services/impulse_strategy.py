"""RETIRED."""
IMP_BOT_ID = "impulse_strategy"
STRATEGY_DESC = STRATEGY_NAME = STRATEGY_VERSION = ""
class ImpulseConfig:
    def __init__(self, **kw):
        for k, v in kw.items(): setattr(self, k, v)
class ImpulseStrategy:
    def __init__(self, *a, **k):
        raise RuntimeError("Impulse retired")


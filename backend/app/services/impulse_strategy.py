
"""RETIRED stub."""
IMP_BOT_ID = "impulse_strategy"
STRATEGY_DESC = STRATEGY_NAME = "Impulse 1D"
STRATEGY_VERSION = "off"

class ImpulseConfig:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

class ImpulseStrategy:
    def __init__(self, *a, **k):
        self._running = False
        self._positions = {}
        self._trade_log = []
    def start(self):
        pass
    def stop(self):
        pass
    def get_status(self):
        return {"running": False}

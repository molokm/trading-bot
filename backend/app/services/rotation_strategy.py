
"""RETIRED stub."""
ROT_BOT_ID = "rotation_strategy"
STRATEGY_DESC = "retired"

class RotationConfig:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

class RotationStrategy:
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

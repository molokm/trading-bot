
"""RETIRED stub."""
BOT_ID = "smart_money"
STRATEGY_NAME = "Умные деньги"
STRATEGY_VERSION = "off"

class TrackerConfig:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

class OKXCopyAPI:
    def __init__(self, *a, **k):
        pass

class SmartMoneyTracker:
    def __init__(self, *a, **k):
        self._running = False
        self._positions = {}
        self._trade_log = []
    def start(self):
        pass
    def stop(self):
        pass
    def get_status(self):
        return {"running": False, "strategy": STRATEGY_NAME}

def get_tracker(*a, **k):
    return None

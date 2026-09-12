
"""RETIRED stub."""
VAL_BOT_ID = "validation_strategy"

def make_validation_config(**kw):
    return type("C", (), kw)()

class ValidationStrategy:
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

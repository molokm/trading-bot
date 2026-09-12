
"""RETIRED stub."""
VWAP_BOT_ID = "vwap_mean_rev"
STRATEGY_NAME = "VWAP Mean Reversion"
STRATEGY_VERSION = "off"
STRATEGY_DESC = "retired"

class ScalpConfig:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

class VWAPMeanReversion:
    def __init__(self, *a, **k):
        self._running = False
        self._positions = {}
        self._trade_log = []
    def start(self):
        pass
    def stop(self):
        pass
    def get_status(self):
        return {"running": False, "strategy": STRATEGY_NAME, "version": "off"}

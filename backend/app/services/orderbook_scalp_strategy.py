
"""RETIRED stub."""
SCALP_BOT_ID = "orderbook_scalp"
STRATEGY_NAME = "Order Book Scalp"
STRATEGY_VERSION = "off"
STRATEGY_DESC = "retired"

class ScalpConfig:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

class OrderBookScalpStrategy:
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

def compute_book_metrics(*a, **k):
    return {}

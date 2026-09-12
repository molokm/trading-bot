
"""RETIRED stub — do not start."""
STRATEGY_NAME = "AI Scale-In 1H (retired)"
AI_SCALE_BOT_ID = "ai_scale_strategy"

class AIScaleConfig:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

class AIScaleStrategy:
    def __init__(self, *a, **k):
        self._running = False
        self._positions = {}
        self._trade_log = []
        self._lifetime_pnl = 0.0
    def start(self):
        print("[AI Scale-In] retired — start ignored", flush=True)
    def stop(self):
        pass
    def get_status(self):
        return {"running": False, "strategy": STRATEGY_NAME, "retired": True}

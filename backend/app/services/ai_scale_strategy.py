"""RETIRED — AI Scale-In removed."""
STRATEGY_NAME = "AI Scale-In 1H (retired)"
AI_SCALE_BOT_ID = "ai_scale_strategy"
class AIScaleConfig:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
class AIScaleStrategy:
    def __init__(self, *a, **k):
        raise RuntimeError("AI Scale-In retired")


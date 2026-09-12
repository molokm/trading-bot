"""RETIRED."""
VAL_BOT_ID = "validation_strategy"
def make_validation_config(**kw):
    return type("C", (), kw)()
class ValidationStrategy:
    def __init__(self, *a, **k):
        raise RuntimeError("Validation retired")


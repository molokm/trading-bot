"""Контрактные спецификации (ctVal / lotSz) должны совпадать с OKX.

28.10 прод: NEAR был открыт размером 810 контрактов вместо ~81 — ровно в 10
раз больше — и потерял -557.72 вместо ~-56. Причина: `CT_VAL["NEAR"]=1.0`, тогда
как на OKX NEAR-USDT-SWAP `ctVal=10` (1 контракт = 10 NEAR). `_size_order`
переводит целевой notional в контракты как `notional / (entry * ctVal)`, поэтому
занижённый ctVal удесятеряет и размер, и риск, и PnL.

Этот тест фиксирует таблицу по фактическим значениям OKX
(`/api/v5/public/instruments`, instType=SWAP), чтобы дрейф больше не проходил
незамеченным.
"""
from app.bots.ai_discretionary.strategy import (
    AIConfig,
    AIStrategy,
    CT_VAL,
    LOT_SZ,
    compare_contract_specs,
)

# coin -> (ctVal, lotSz) — как отдаёт OKX для USDT-M SWAP на 2026-10.
OKX_TRUTH = {
    "BTC": (0.01, 0.01),
    "ETH": (0.1, 0.01),
    "SOL": (1.0, 0.01),
    "XRP": (100.0, 0.01),
    "OKB": (0.01, 1.0),
    "DOGE": (1000.0, 0.01),
    "BCH": (0.1, 0.1),
    "LINK": (1.0, 0.1),
    "AVAX": (1.0, 0.1),
    "ADA": (100.0, 0.1),
    "LTC": (1.0, 0.1),
    "DOT": (1.0, 1.0),
    "UNI": (1.0, 1.0),
    "NEAR": (10.0, 0.1),
}


def test_ct_val_matches_okx_instrument_specs():
    for coin, (ct, lot) in OKX_TRUTH.items():
        assert CT_VAL[coin] == ct, f"{coin}: ctVal {CT_VAL[coin]} != OKX {ct}"
        assert LOT_SZ[coin] == lot, f"{coin}: lotSz {LOT_SZ[coin]} != OKX {lot}"


def test_near_contract_value_is_ten_not_one():
    # Явный регресс на инцидент: 1 контракт NEAR = 10 NEAR, а не 1.
    assert CT_VAL["NEAR"] == 10.0


def test_okb_contract_value_is_fractional():
    # OKB на OKX — дробный контракт; в коде раньше стояло 1.0 (ошибка x100).
    assert CT_VAL["OKB"] == 0.01


def _bot(equity=9507.0):
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot._latest_indicators = {}
    bot._equity = equity
    return bot


def test_near_sizing_is_not_10x_oversized():
    """Позиция того же профиля риска, что 28.10, теперь ~81, а не ~810."""
    bot = _bot()
    # equity $9.5k, риск 1.2% -> ~$114; при стопе ~2.95% это ~$3.9k notional.
    sz, _lev = bot._size_order("NEAR", 4.7715, 0.0295, equity=9507.0)

    assert 70 <= sz <= 90, f"NEAR size {sz} — ожидалось ~81, не 810"
    notional = sz * CT_VAL["NEAR"] * 4.7715
    assert 3_000 <= notional <= 5_000, f"NEAR notional {notional:.0f}"


def test_near_ten_x_bug_would_be_caught_by_the_table_bootstrapping():
    """Проверяем саму арифметику ошибки: ctVal=1 дал бы ровно 10x контрактов."""
    bot = _bot()
    good, _ = bot._size_order("NEAR", 4.7715, 0.0295, equity=9507.0)

    # Каким был бы размер при прежнем ctVal=1: он ровно в 10 раз больше.
    assert abs((good * 10) - 810) < 60, "связь 10x должна воспроизводиться"


# ── Runtime-валидация против OKX ──────────────────────────────────────────

def _okx_row(coin, ct, lot):
    return {"instId": f"{coin}-USDT-SWAP", "ctVal": str(ct), "lotSz": str(lot)}


def test_compare_contract_specs_ok_when_the_table_matches_okx():
    rows = [_okx_row(c, ct, lot) for c, (ct, lot) in OKX_TRUTH.items()]
    assert compare_contract_specs(rows, coins=list(OKX_TRUTH)) == []


def test_compare_contract_specs_flags_the_near_1ctval_regression():
    # Ровно та ошибка, что стоила 557 USDT: в коде 10, на OKX 1.
    out = compare_contract_specs([_okx_row("NEAR", 1.0, 0.1)], coins=["NEAR"])
    assert len(out) == 1
    assert out[0]["coin"] == "NEAR"
    assert out[0]["field"] == "ctVal"
    assert out[0]["code"] == 10.0 and out[0]["okx"] == 1.0


def test_compare_contract_specs_flags_lot_size_drift():
    # OKB lotSz=1.0 на OKX; строка с 0.1 должна попасть в отчёт.
    out = compare_contract_specs([_okx_row("OKB", 0.01, 0.1)], coins=["OKB"])
    assert [(m["coin"], m["field"]) for m in out] == [("OKB", "lotSz")]


def test_compare_contract_specs_ignores_coins_missing_on_okx():
    # DAI есть в CT_VAL, но не торгуется на OKX — молча пропускаем.
    assert compare_contract_specs([], coins=["DAI"]) == []

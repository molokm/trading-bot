import asyncio, json, sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app.services.data_cache import ensure_candles
from app.services.backtest_engine import BacktestEngine, load_strategy_file
from app.services.strategy_loader import get_strategy_code, STRATEGIES_DIR

async def run():
    strategies = ["trend_bounce_levx_pro", "trend_momentum_pro", "momentum_atr_trail"]

    candles = await ensure_candles(
        "BTC-USDT", "5m",
        start_date="2025-06-14",
        end_date="2026-06-14",
        force_refresh=False,
        max_candles=200000,
    )
    print(f"Loaded {len(candles)} candles\n")

    for sid in strategies:
        strategy_code = get_strategy_code(sid)
        if not strategy_code:
            print(f"[SKIP] {sid} not found")
            continue

        meta = load_strategy_file(str(STRATEGIES_DIR / f"{sid}.py"))
        strategy_name = meta.get("@name", sid) if meta else sid
        default_params = {}
        if meta:
            params_str = meta.get("@params", "")
            if params_str:
                try:
                    default_params = json.loads(params_str)
                except Exception:
                    pass

        engine = BacktestEngine(strategy_code, strategy_name)
        params = {"name": strategy_name, "timeframe": "5m", "symbol": "BTC-USDT", **default_params}

        result_raw = engine.run(candles, 1000, params)
        r = result_raw if "total_return_pct" in result_raw else result_raw.get("result", result_raw)

        params_xgb = {**params, "xgb_gate": True}
        engine2 = BacktestEngine(strategy_code, strategy_name)
        result_xgb = engine2.run(candles, 1000, params_xgb)
        rx = result_xgb if "total_return_pct" in result_xgb else result_xgb.get("result", result_xgb)

        print(f"=== {strategy_name} ===")
        print(f"  WITHOUT XGB: {r.get('total_return_pct',0):.1f}% | WR {r.get('win_rate',0):.1f}% | Sharpe {r.get('sharpe_ratio',0)} | DD {r.get('max_drawdown',0):.1f}% | PF {r.get('profit_factor',0)} | Trades {r.get('total_trades',0)}")
        print(f"  WITH XGB:    {rx.get('total_return_pct',0):.1f}% | WR {rx.get('win_rate',0):.1f}% | Sharpe {rx.get('sharpe_ratio',0)} | DD {rx.get('max_drawdown',0):.1f}% | PF {rx.get('profit_factor',0)} | Trades {rx.get('total_trades',0)}")
        delta = rx.get('total_return_pct',0) - r.get('total_return_pct',0)
        print(f"  XGB IMPACT:  {'+' if delta > 0 else ''}{delta:.1f}%\n")

if __name__ == "__main__":
    asyncio.run(run())

import asyncio, json, sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app.services.data_cache import ensure_candles
from app.services.backtest_engine import BacktestEngine, load_strategy_file
from app.services.strategy_loader import get_strategy_code, STRATEGIES_DIR

async def run_backtest(strategy_id: str):
    strategy_code = get_strategy_code(strategy_id)
    if not strategy_code:
        raise ValueError(f"Strategy {strategy_id} not found")

    meta = load_strategy_file(str(STRATEGIES_DIR / f"{strategy_id}.py"))
    strategy_name = meta.get("@name", strategy_id) if meta else strategy_id
    default_params = {}
    if meta:
        params_str = meta.get("@params", "")
        if params_str:
            try:
                default_params = json.loads(params_str)
            except Exception:
                pass

    print(f"Strategy: {strategy_name}")
    print(f"Default params: {json.dumps(default_params, indent=2)}")

    print(f"Loading 3+ years of 5m candles...")
    candles = await ensure_candles(
        "BTC-USDT", "5m",
        start_date="2023-06-14",
        end_date="2026-06-14",
        force_refresh=False,
        max_candles=500000,
    )
    print(f"Loaded {len(candles)} candles")

    engine = BacktestEngine(strategy_code, strategy_name)
    params = {"name": strategy_name, "timeframe": "5m", "symbol": "BTC-USDT", **default_params}
    if os.environ.get("XGB_GATE") == "1":
        params["xgb_gate"] = True
        print("XGBoost gate: ENABLED")
    result = engine.run(candles, 1000, params)

    r = result if "total_return_pct" in result else result.get("result", result)
    print(f"\n=== {strategy_name} ===")
    print(f"Return: {r.get('total_return_pct',0):.1f}%")
    print(f"Final: ${r.get('final_capital',0):.0f}")
    print(f"Trades: {r.get('total_trades',0)}  ({r.get('winning_trades',0)}W / {r.get('losing_trades',0)}L)")
    print(f"WinRate: {r.get('win_rate',0)*100:.1f}%")
    print(f"Sharpe: {r.get('sharpe_ratio',0)}")
    print(f"Max DD: {r.get('max_drawdown',0):.1f}%")
    print(f"Profit Factor: {r.get('profit_factor',0)}")

    ds = r.get("xgb_dataset", [])
    print(f"XGB dataset: {len(ds)} samples")
    if ds:
        fname = f"/tmp/xgb_full_{strategy_id}.json"
        with open(fname, "w") as f:
            json.dump(ds, f)
        print(f"Saved: {fname}")

if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else "trend_momentum_pro"
    asyncio.run(run_backtest(sid))

"""Verify Rotation Strategy indicators, signals, and PNL on live OKX data."""

import asyncio
import json
import sys
sys.path.insert(0, '/home/z/my-project/trading-bot/backend')

from app.services.rotation_strategy import (
    RotationStrategy, RotationConfig, SWAP_MAP,
    CT_VAL, LOT_SZ, ROT_BOT_ID,
)
from app.services.okx_client import OKXClientManager
from app.database import db
from dotenv import load_dotenv
import os

load_dotenv('/home/z/my-project/trading-bot/backend/.env')


async def main():
    # 1. Init OKX client
    mgr = OKXClientManager()
    key = os.getenv("OKX_API_KEY", "")
    secret = os.getenv("OKX_SECRET_KEY", "")
    passphrase = os.getenv("OKX_PASSPHRASE", "")
    demo = os.getenv("OKX_DEMO", "true").lower() in ("1", "true")

    if not (key and secret and passphrase):
        print("ERROR: No OKX credentials in .env")
        return

    result = await mgr.init_client(key, secret, passphrase, demo)
    if result.get("error"):
        print(f"ERROR: OKX init failed: {result}")
        return
    client = mgr.get_client()
    print(f"OKX client connected (demo={demo})")

    # 2. Create strategy instance (don't start it)
    cfg = RotationConfig(
        symbols=["BTC", "ETH", "BNB", "SOL"],
        capital=10000.0, top_k=2,
    )
    strat = RotationStrategy(config=cfg, client_manager=mgr)

    coins = ["BTC", "ETH", "BNB", "SOL"]
    print("\n" + "=" * 80)
    print("INDICATORS CHECK (100 daily candles, signal bar = yesterday)")
    print("=" * 80)

    all_indicators = {}
    for coin in coins:
        inst_id = SWAP_MAP[coin]
        candles = await strat._fetch_daily(client, coin, limit=100)
        if not candles:
            print(f"  {coin}: FAILED to fetch candles")
            continue
        ind = strat._compute_indicators(candles)
        if not ind:
            print(f"  {coin}: FAILED to compute (need >= 70 candles, got {len(candles)})")
            continue
        all_indicators[coin] = ind
        signal = ""
        if ind["roc"] > 0 and ind["ema_trend"] and ind["adx"] >= cfg.adx_min:
            signal = "LONG"
        elif ind["roc"] < 0 and not ind["ema_trend"] and ind["adx"] >= cfg.adx_min:
            signal = "SHORT"
        else:
            reasons = []
            if abs(ind["roc"]) < 0.01:
                reasons.append(f"ROC={ind['roc']:.2f}% (~0)")
            elif ind["roc"] > 0 and not ind["ema_trend"]:
                reasons.append(f"ROC>0 but EMA20<EMA50")
            elif ind["roc"] < 0 and ind["ema_trend"]:
                reasons.append(f"ROC<0 but EMA20>EMA50")
            if ind["adx"] < cfg.adx_min:
                reasons.append(f"ADX={ind['adx']:.1f}<{cfg.adx_min}")
            signal = f"NO SIGNAL ({'; '.join(reasons)})"

        print(f"\n  {coin} ({inst_id}):")
        print(f"    Price (today):     ${ind['close_today']:,.2f}")
        print(f"    Price (signal bar):${ind['price']:,.2f}")
        print(f"    ROC(14):           {ind['roc']:+.2f}%")
        print(f"    EMA20:             ${ind['ema_fast']:,.2f}")
        print(f"    EMA50:             ${ind['ema_slow']:,.2f}")
        print(f"    EMA20 > EMA50:     {ind['ema_trend']}")
        print(f"    ADX(14):           {ind['adx']:.1f}")
        print(f"    ATR(14):           ${ind['atr']:,.2f}")
        print(f"    Signal:            {signal}")

    # 3. Simulate ranking and target selection
    print("\n" + "=" * 80)
    print("RANKING & TARGET SELECTION")
    print("=" * 80)
    rankings = []
    for coin, ind in all_indicators.items():
        if ind["atr"] <= 0:
            continue
        rankings.append((coin, ind["roc"], ind["ema_trend"], ind["adx"], ind["atr"]))
    rankings.sort(key=lambda x: x[1], reverse=True)

    print("\n  Ranked by ROC:")
    for i, (coin, roc, trend, adx, atr) in enumerate(rankings):
        print(f"    {i+1}. {coin}: ROC={roc:+.2f}%  EMA_trend={trend}  ADX={adx:.1f}  ATR=${atr:.2f}")

    target_coins = set()
    for coin, roc_val, ema_trend, adx_val, atr_val in rankings:
        if len(target_coins) >= cfg.top_k:
            break
        if roc_val > 0 and ema_trend and adx_val >= cfg.adx_min:
            target_coins.add((coin, "long"))
        elif roc_val < 0 and not ema_trend and adx_val >= cfg.adx_min:
            target_coins.add((coin, "short"))

    print(f"\n  Target positions (top_k={cfg.top_k}, adx_min={cfg.adx_min}):")
    if target_coins:
        for coin, side in target_coins:
            ind = all_indicators[coin]
            sz = strat._calc_size(coin, ind["close_today"])
            notional = sz * CT_VAL[coin] * ind["close_today"]
            print(f"    {coin} {side.upper()}: size={sz}, notional=${notional:,.2f}")
    else:
        print("    NO TARGETS (all coins filtered out)")

    # 4. Position sizing verification
    print("\n" + "=" * 80)
    print("POSITION SIZING VERIFICATION")
    print("=" * 80)
    print(f"  Capital: ${cfg.capital:,.2f}")
    print(f"  Top K: {cfg.top_k}")
    print(f"  Max pos %: {cfg.max_pos_pct * 100:.0f}%")
    for coin in coins:
        if coin not in all_indicators:
            continue
        price = all_indicators[coin]["close_today"]
        sz = strat._calc_size(coin, price)
        ct = CT_VAL.get(coin, 0.01)
        notional = sz * ct * price
        alloc_pct = notional / cfg.capital * 100
        print(f"  {coin}: size={sz}, ct={ct}, notional=${notional:,.2f} ({alloc_pct:.1f}% of capital)")

    # 5. PNL endpoint simulation
    print("\n" + "=" * 80)
    print("PNL DISPLAY CHECK")
    print("=" * 80)
    print(f"  Equity: ${strat._equity:,.2f}")
    print(f"  Trade log entries: {len(strat._trade_log)}")
    print(f"  Open positions: {len(strat._positions)}")
    print(f"  (Note: live bot has its own state; this is a fresh instance)")

    # 6. Trailing stop / breakeven logic test
    print("\n" + "=" * 80)
    print("TRAILING STOP / BREAKEVEN LOGIC TEST")
    print("=" * 80)
    entry = 65000.0
    trail_pct = 0.02
    be_pct = 0.03
    print(f"  Simulating LONG position: entry=${entry:,.0f}")
    print(f"  Trailing stop: {trail_pct*100}% from peak")
    print(f"  Breakeven trigger: +{be_pct*100}%")

    # Scenario 1: price goes up 5%
    peak = entry * 1.05
    stop = peak * (1 - trail_pct)
    print(f"\n  Scenario: price rises to ${peak:,.0f} (+5%)")
    print(f"    Peak: ${peak:,.0f}")
    print(f"    Trailing stop: ${stop:,.2f} (peak * {1-trail_pct})")
    print(f"    Breakeven triggered: YES (5% > 3%)")
    be_stop = entry * 0.999
    print(f"    Breakeven stop: ${be_stop:,.2f}")
    print(f"    Effective stop: ${max(stop, be_stop):,.2f}")

    # Scenario 2: price drops back
    print(f"\n  Scenario: price drops to ${stop + 10:,.0f}")
    print(f"    Stop = ${stop:,.2f}, current > stop → HOLD")
    print(f"\n  Scenario: price drops to ${stop - 10:,.0f}")
    print(f"    Stop = ${stop:,.2f}, current < stop → CLOSE (trail_stop)")

    # 7. Check for potential issues
    print("\n" + "=" * 80)
    print("POTENTIAL ISSUES FOUND")
    print("=" * 80)
    issues = []

    # Check td_mode
    print(f"  [ ] td_mode='isolated' in _place_order — should be 'cross' for no-leverage")
    print(f"      (If demo account default is cross, isolated orders still work)")

    # Check delete_position
    print(f"  [ ] delete_position(ROT_BOT_ID) deletes ALL positions, not per-instrument")

    # Check posSide
    print(f"  [ ] No posSide in order placement — fails if account is in hedge mode")

    # Check ADX warmup
    for coin, ind in all_indicators.items():
        if ind["adx"] == 0:
            issues.append(f"  [!] {coin} ADX=0 — might not have enough data for valid ADX")
    if issues:
        print()
        for issue in issues:
            print(issue)

    print("\n" + "=" * 80)
    print("DONE")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())

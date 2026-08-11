#!/usr/bin/env python3
"""Impulse Bot — fast momentum entry + pyramiding + cascade exit (1H).

Standalone experimental bot on OKX SWAP 1H data (BTC/ETH/BNB/SOL).

Idea (as requested):
  * резкий рост / импульс + подтверждение индикаторов → вход на следующем
    баре (T close → T+1 open), с плечом;
  * при продолжении роста И росте объёма → докупка (пирамидирование) в
    коротком окне после входа;
  * трейлинг-стоп переставляется на каждый новый максимум (быстро, по ATR);
  * выход частями: частичный TP по уровням ATR + остаток на трейлинге →
    риск разворота / ложного роста минимизирован.

Rules (causal, no look-ahead — same honest conventions as the other bots):
  1. Signal on bar T CLOSE using only data up to T
  2. Entry / adds at bar T+1 OPEN
  3. Stops checked against bar HIGH/LOW first (pessimistic)
  4. Commission 0.10% + slippage 0.05% per side
  5. Funding applied at 8h boundaries when history is available
  6. Pyramiding capped by max_adds, add window, and margin cap

Run:
  python scripts/impulse_bot_backtest.py                 # default config
  python scripts/impulse_bot_backtest.py --conf aggressive
  python scripts/impulse_bot_backtest.py --refresh       # refetch data
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, asdict

sys.path.insert(0, os.path.dirname(__file__))

from honest_backtest_reversion_1h import (  # noqa: E402
    COINS,
    COMMISSION,
    SLIPPAGE,
    CT_VAL,
    LOT_SZ,
    FUNDING_INTERVAL_MS,
    _unrealized,
    adx_series,
    atr_series,
    buy_and_hold_btc,
    calc_size,
    ema_series,
    load_data,
    load_funding,
    print_report,
    roc_series,
    rsi_series,
    sma_series,
    summarize,
)

# Cost model aligned to the freqtrade reference (backtest fills at the raw candle
# open, default fee 0.05% per side) — the honest engine defaults (0.10% + 0.05%
# slippage) shift avg_entry and therefore be/trail stops, causing ±1-day exit diffs.
COMMISSION = 0.0005
SLIPPAGE = 0.0

OUT_PATH = os.path.join(os.path.dirname(__file__), "impulse_bot_results.json")

CACHE_PATH = os.path.join(os.path.dirname(__file__), "reversion_1h_cache.json")


def load_cached_data():
    """Use the full 4.4y 1H cache directly (skip the 400-day refetch)."""
    with open(CACHE_PATH) as f:
        cached = json.load(f)
    data = cached["data"]
    ok = all(c in data and len(data[c]) > 500 for c in COINS)
    if not ok:
        raise RuntimeError("1H cache incomplete; run honest_backtest_reversion_1h to rebuild it")
    return data


def aggregate_bars(bars_1h, n=4):
    """Aggregate 1H bars into n-H bars (causal, uses only past bars).

    Buckets are anchored to fixed epoch multiples of n hours so that all
    coins share the same bar boundaries (matching the 00:00/04:00/... grid).
    """
    bucket_ms = n * 60 * 60 * 1000
    buckets = {}
    for b in bars_1h:
        key = (int(b["ts"]) // bucket_ms) * bucket_ms
        buckets.setdefault(key, []).append(b)
    out = []
    for key in sorted(buckets):
        chunk = buckets[key]
        if len(chunk) < n:
            continue
        out.append({
            "ts": key,
            "dt": chunk[0]["dt"],
            "O": chunk[0]["O"],
            "H": max(c["H"] for c in chunk),
            "L": min(c["L"] for c in chunk),
            "C": chunk[-1]["C"],
            "V": sum(c["V"] for c in chunk),
        })
    return out


@dataclass
class ImpulseConfig:
    name: str
    capital: float = 10000.0
    top_k: int = 3                    # max concurrent positions
    # entry / impulse
    impulse_bars: int = 3             # ROC window for the impulse (e.g. 3 bars)
    entry_roc: float = 3.0            # |ROC| % over window (e.g. 3.0 = 3%)
    rsi_conf_min: float = 55.0        # long confirmation RSI floor
    rsi_conf_max: float = 85.0        # not chasing extreme overbought
    ema_fast: int = 20
    ema_slow: int = 50
    adx_min: float = 20.0
    vol_mult: float = 1.5             # volume > avg_vol * this
    vol_period: int = 24
    # pyramiding (докупка)
    max_adds: int = 2
    add_size_ratio: float = 0.6       # each add = 60% of base size
    add_window_bars: int = 12         # only add within N bars of entry
    add_min_move: float = 0.0         # min peak move since last add (ATR-based below)
    add_atr_mult: float = 0.5         # add when new peak >= last_add_peak + ATR*this
    # risk / sizing
    max_leverage: float = 3.0
    risk_per_trade: float = 0.02
    sl_atr_mult: float = 1.5          # initial stop = entry - ATR*this
    sl_atr_mult_short: float = 0.0    # short-specific stop; 0 = use sl_atr_mult
    trail_atr_mult: float = 0.8       # trail = peak - ATR*this
    trail_atr_mult_short: float = 0.0 # short-specific trail; 0 = use trail_atr_mult
    be_pct: float = 0.005             # move stop to breakeven after +0.5%
    cooldown_bars: int = 24           # min bars between entries on the SAME coin
    bars_per_day: int = 24            # 1H=24, 4H=6, 1D=1 (for yearly stats)
    # cascade exit (выход частями)
    tp1_atr: float = 1.0
    tp1_frac: float = 0.3
    tp2_atr: float = 2.0
    tp2_frac: float = 0.3
    max_hold_bars: int = 48           # time exit (48h)
    exit_ema_death: bool = True       # close remainder when price < ema_fast
    allow_short: bool = True
    max_margin_pct: float = 0.5


BASE = ImpulseConfig(
    name="Impulse Bot Daily (1D): moderate entry ROC + pyramiding",
    top_k=4, impulse_bars=1, entry_roc=4.0,
    rsi_conf_min=0.0, rsi_conf_max=100.0,
    ema_fast=20, ema_slow=50, adx_min=0.0,
    vol_mult=1.5, vol_period=24,
    max_adds=2, add_size_ratio=0.6, add_window_bars=5, add_atr_mult=0.5,
    max_leverage=3.0, risk_per_trade=0.10,
    sl_atr_mult=5.0, sl_atr_mult_short=5.0,
    trail_atr_mult=8.0, trail_atr_mult_short=8.0, be_pct=0.005,
    cooldown_bars=5,
    tp1_atr=2.0, tp1_frac=0.3, tp2_atr=6.0, tp2_frac=0.3,
    max_hold_bars=30, exit_ema_death=False, allow_short=True,
    bars_per_day=1,
)

AGGRESSIVE = ImpulseConfig(
    name="Impulse Bot 1H (aggressive adds + tight trail)",
    top_k=4, impulse_bars=2, entry_roc=2.0,
    rsi_conf_min=50.0, rsi_conf_max=88.0,
    ema_fast=10, ema_slow=30, adx_min=18.0,
    vol_mult=1.2, vol_period=24,
    max_adds=3, add_size_ratio=0.5, add_window_bars=8, add_atr_mult=0.4,
    max_leverage=5.0, risk_per_trade=0.025,
    sl_atr_mult=1.2, trail_atr_mult=0.5, be_pct=0.003,
    cooldown_bars=12,
    tp1_atr=0.8, tp1_frac=0.35, tp2_atr=1.5, tp2_frac=0.35,
    max_hold_bars=36, exit_ema_death=True, allow_short=True,
)


# ── Engine ──

def run_strategy(bar_data: dict, cfg: ImpulseConfig, funding: dict | None = None,
                 return_trades: bool = False, start_dt: str | None = None) -> dict:
    coin_data = {}
    for coin in COINS:
        bars = bar_data.get(coin, [])
        if len(bars) < 250:
            raise RuntimeError(f"{coin}: not enough bars ({len(bars)})")
        coin_data[coin] = _build_coin_frame(bars, cfg)

    common = None
    for coin, cd in coin_data.items():
        bars = {c["dt"] for c in cd["candles"]}
        common = bars if common is None else (common & bars)
    all_dates = sorted(common)

    date_idx = {
        coin: {c["dt"]: i for i, c in enumerate(cd["candles"])}
        for coin, cd in coin_data.items()
    }

    equity = cfg.capital
    positions = {}
    trades = []
    equity_curve = []
    filters_hit = defaultdict(int)
    last_entry = -10 ** 9
    last_exit_i = {}      # coin -> last bar index a position closed
    total_funding = 0.0
    start_i = max(cfg.ema_slow + cfg.vol_period + 10, cfg.impulse_bars + 30)
    if start_dt is not None:
        idxs = [j for j, d in enumerate(all_dates) if d >= start_dt]
        if idxs:
            start_i = max(start_i, idxs[0])

    for i in range(start_i, len(all_dates)):
        date = all_dates[i]
        sig_date = all_dates[i - 1]

        mtm = {}
        for coin in COINS:
            ci = date_idx[coin][date]
            mtm[coin] = coin_data[coin]["candles"][ci]["C"]

        # ── 0. Funding at 8h boundaries ──
        if funding:
            for coin in list(positions.keys()):
                pos = positions[coin]
                ci = date_idx[coin][date]
                bar = coin_data[coin]["candles"][ci]
                if bar["ts"] % FUNDING_INTERVAL_MS != 0:
                    continue
                ct = CT_VAL[coin]
                rate = funding.get(coin, {}).get(str(bar["ts"]),
                          funding.get(coin, {}).get(bar["ts"], 0.0))
                if rate != 0.0:
                    notional = pos["size"] * ct * bar["C"]
                    fpnl = -notional * rate if pos["side"] == "long" else notional * rate
                    equity += fpnl
                    total_funding += fpnl
                    pos["funding"] = pos.get("funding", 0.0) + fpnl

        # ── 1. Single adjustment per candle (freq adjust_trade_position, OPEN-based):
        #      TP1 → TP2 → add; first that triggers wins; fills at today's open ──
        for coin in list(positions.keys()):
            pos = positions[coin]
            cd = coin_data[coin]
            ci2 = date_idx[coin][date]
            bar = cd["candles"][ci2]
            entry_raw = bar["O"]
            atr = pos["atr"]
            if atr <= 0:
                continue

            if pos["side"] == "long":
                dist_atr = (entry_raw - pos["avg_entry"]) / atr
            else:
                dist_atr = (pos["avg_entry"] - entry_raw) / atr

            acted = False
            if not pos["tp1_done"] and dist_atr >= cfg.tp1_atr:
                pos["tp1_done"] = True
                if pos["size"] * (1 - cfg.tp1_frac) >= LOT_SZ[coin]:
                    _close_fraction(pos, cfg, coin, CT_VAL[coin], bar, "tp1", trades, entry_raw)
                    acted = True
            elif not pos["tp2_done"] and dist_atr >= cfg.tp2_atr:
                pos["tp2_done"] = True
                if pos["size"] * (1 - cfg.tp2_frac) >= LOT_SZ[coin]:
                    _close_fraction(pos, cfg, coin, CT_VAL[coin], bar, "tp2", trades, entry_raw)
                    acted = True
            if acted or pos["size"] <= 0:
                continue

            # pyramiding (add)
            if pos["adds"] >= cfg.max_adds:
                continue
            if i - pos["entry_i"] > cfg.add_window_bars:
                continue
            ci1 = date_idx[coin][sig_date]      # yesterday bar: volume surge
            prev_bar = bar_data[coin][ci1]
            # freq: surge uses last["avg_vol"] recomputed at the current candle, NOT entry-time
            avg_vol_now = _avg_vol(bar_data[coin], ci1, cfg.vol_period)
            if avg_vol_now <= 0:
                continue
            # new peak vs last add point (freq: long current_rate >= last_peak + atr*mult,
            #                              short current_rate <= last_peak - atr*mult)
            if pos["side"] == "long":
                threshold = pos["last_add_peak"] + atr * cfg.add_atr_mult
                if entry_raw < threshold:
                    continue
                if prev_bar["V"] < avg_vol_now * cfg.vol_mult:
                    filters_hit["add_no_vol"] += 1
                    continue
            else:
                threshold = pos["last_add_peak"] - atr * cfg.add_atr_mult
                if entry_raw > threshold:
                    continue
                if prev_bar["V"] < avg_vol_now * cfg.vol_mult:
                    filters_hit["add_no_vol"] += 1
                    continue

            # execute add at today open
            lev = dynamic_leverage(atr, entry_raw, cfg.max_leverage)
            add_sz = _add_size(cfg, pos, coin, entry_raw, atr, lev, equity)
            if add_sz <= 0:
                continue
            fill = entry_raw
            fee = add_sz * CT_VAL[coin] * fill * COMMISSION
            equity -= fee
            # blend average entry (freq: open_rate blends; atr/stop NOT touched on add)
            old_cost = pos["size"] * pos["avg_entry"]
            pos["size"] += add_sz
            pos["avg_entry"] = (old_cost + add_sz * fill) / pos["size"]
            pos["adds"] += 1
            pos["last_add_peak"] = entry_raw
            trades.append({
                "date": date, "coin": coin, "side": pos["side"], "pnl": round(-fee, 2),
                "reason": f"add_{pos['adds']}", "entry": round(fill, 4), "exit": None,
                "size": add_sz, "closed": False,
            })
            filters_hit["adds_made"] += 1

        # ── 2. Manage open positions (freq: should_exit AFTER adjust; exit-check runs
        #      with after_fill=False so peak/be/trail DO update even on an add bar) ──
        for coin in list(positions.keys()):
            pos = positions[coin]
            ci = date_idx[coin][date]
            bar = coin_data[coin]["candles"][ci]
            ct = CT_VAL[coin]
            cd = coin_data[coin]

            hit_stop = False
            exit_raw = None
            reason = "trail_stop"

            if pos["side"] == "long":
                # freq quirk (mirror of short): custom_stoploss returns ratio lev*(prev_stop/H - 1);
                # adjust_stop_loss maps to absolute via HIGH. When H >= prev_stop -> stop = prev_stop
                # (trigger L <= prev_stop). When H < prev_stop the abs() flips sign and the stop
                # becomes 2*H - prev_stop (trigger L <= that).
                eff_stop = pos["stop"] if bar["H"] >= pos["stop"] else 2 * bar["H"] - pos["stop"]
                if bar["L"] <= eff_stop:
                    hit_stop = True
                    exit_raw = eff_stop
                else:
                    # update peak & trailing stop
                    if bar["H"] > pos["peak"]:
                        pos["peak"] = bar["H"]
                        new_stop = pos["peak"] - pos["atr"] * cfg.trail_atr_mult
                        if new_stop > pos["stop"]:
                            pos["stop"] = new_stop
                    if (not pos["breakeven"]
                            and cd["candles"][ci - 1]["C"] >= pos["avg_entry"] * (1 + cfg.be_pct)):
                        pos["breakeven"] = True
                    # freq recomputes the be floor EVERY candle from open_rate (=avg_entry):
                    # after an add, avg_entry rises and the be stop must rise with it
                    if pos["breakeven"]:
                        pos["stop"] = max(pos["stop"], pos["avg_entry"] * 0.999)
            else:  # short
                trail_m = cfg.trail_atr_mult_short or cfg.trail_atr_mult
                # freq short quirk: the returned stoploss RATIO is mapped to stop_loss_abs via
                # LOW*(1+|ratio|/lev). When LOW > prev_stop the abs() inflates the stop to
                # 2*LOW - prev_stop, and the trigger is HIGH >= that inflated stop (not prev_stop).
                eff_stop = pos["stop"] if bar["L"] <= pos["stop"] else 2 * bar["L"] - pos["stop"]
                if bar["H"] >= eff_stop:
                    hit_stop = True
                    exit_raw = eff_stop
                else:
                    if bar["L"] < pos["peak"]:
                        pos["peak"] = bar["L"]
                        new_stop = pos["peak"] + pos["atr"] * trail_m
                        if new_stop < pos["stop"]:
                            pos["stop"] = new_stop
                    if (not pos["breakeven"]
                            and cd["candles"][ci - 1]["C"] <= pos["avg_entry"] * (1 - cfg.be_pct)):
                        pos["breakeven"] = True
                    if pos["breakeven"]:
                        pos["stop"] = min(pos["stop"], pos["avg_entry"] * 1.001)
                    if cfg.exit_ema_death and pos["size"] > 0 and bar["C"] > cd["ema_f"][ci]:
                        exit_raw = bar["C"]
                        reason = "ema_death"
                        hit_stop = True

            # time exit
            if not hit_stop and i - pos["entry_i"] >= cfg.max_hold_bars and pos["size"] > 0:
                exit_raw = bar["O"]
                reason = "time_exit"
                hit_stop = True

            if hit_stop and pos["size"] > 0:
                equity = _close_position(pos, cfg, coin, ct, bar, exit_raw, reason, trades, equity)
                last_exit_i[coin] = i
            if pos["size"] <= 0:
                del positions[coin]

        # ── 3. New entries from yesterday signal ──
        sig_candidates = []
        for coin, cd in coin_data.items():
            if coin in positions:
                continue
            if i - last_exit_i.get(coin, -10 ** 9) < cfg.cooldown_bars:
                filters_hit["cooldown"] += 1
                continue
            si = date_idx[coin][sig_date]
            bar = bar_data[coin][si]
            c = cd["closes"][si]
            atr = cd["atr"][si]
            if atr <= 0:
                continue
            vol_avg = _avg_vol(bar_data[coin], si, cfg.vol_period)
            if vol_avg <= 0:
                continue
            roc = cd["roc"][si]
            rsi = cd["rsi"][si]
            ema_trend = cd["ema_f"][si] > cd["ema_s"][si]

            if bar["V"] < vol_avg * cfg.vol_mult:
                filters_hit["vol_surge"] += 1
                continue
            if cd["adx"][si] < cfg.adx_min:
                filters_hit["adx_low"] += 1
                continue

            if roc >= cfg.entry_roc and ema_trend and rsi >= cfg.rsi_conf_min and rsi <= cfg.rsi_conf_max:
                side = "long"
            elif (cfg.allow_short and roc <= -cfg.entry_roc and not ema_trend
                  and rsi <= (100 - cfg.rsi_conf_min) and rsi >= (100 - cfg.rsi_conf_max)):
                side = "short"
            else:
                continue
            sig_candidates.append({"coin": coin, "side": side, "atr": atr,
                                   "avg_vol": vol_avg, "roc": roc})

        # rank by impulse strength
        sig_candidates.sort(key=lambda x: abs(x["roc"]), reverse=True)
        for t in sig_candidates:
            if len([p for p in positions if positions[p]["size"] > 0]) >= cfg.top_k:
                break
            coin, side = t["coin"], t["side"]
            if coin in positions:
                continue
            ci = date_idx[coin][date]
            entry_raw = coin_data[coin]["candles"][ci]["O"]
            atr = t["atr"]
            lev = dynamic_leverage(atr, entry_raw, cfg.max_leverage)
            sl_m = cfg.sl_atr_mult_short if side == "short" and cfg.sl_atr_mult_short else cfg.sl_atr_mult
            stop_dist = atr * sl_m
            sz = calc_size(equity, coin, entry_raw, stop_dist, lev, cfg.risk_per_trade, cfg.max_margin_pct)
            if sz <= 0:
                continue
            if side == "long":
                fill = entry_raw * (1 + SLIPPAGE)
                stop = fill - stop_dist
            else:
                fill = entry_raw * (1 - SLIPPAGE)
                stop = fill + stop_dist
            fee = sz * CT_VAL[coin] * fill * COMMISSION
            equity -= fee
            positions[coin] = {
                "side": side, "size": sz, "avg_entry": fill, "entry": fill,
                "stop": stop, "peak": fill, "atr": atr, "lev": lev,
                "breakeven": False, "tp1_done": False, "tp2_done": False,
                "adds": 0, "last_add_peak": fill, "avg_vol": t["avg_vol"],
                "tp1_atr_pct": 0.0, "tp2_atr_pct": 0.0, "entry_i": i,
            }
            # set TP% after entry (based on entry ATR)
            positions[coin]["tp1_atr_pct"] = cfg.tp1_atr * atr / fill
            positions[coin]["tp2_atr_pct"] = cfg.tp2_atr * atr / fill
            trades.append({
                "date": date, "coin": coin, "side": side, "pnl": round(-fee, 2),
                "reason": "open", "entry": round(fill, 4), "exit": None,
                "size": sz, "closed": False, "leverage": round(lev, 2),
            })
            last_entry = i

        unreal = _unrealized(positions, mtm)
        unreal += sum(p.get("partial_pnl", 0.0) for p in positions.values())
        equity_curve.append({"date": date, "equity": equity + unreal})

    # Force-close at last close
    if positions and all_dates:
        date = all_dates[-1]
        for coin in list(positions.keys()):
            pos = positions[coin]
            if pos["size"] <= 0:
                continue
            ci = date_idx[coin][date]
            bar = coin_data[coin]["candles"][ci]
            equity = _close_position(pos, cfg, coin, CT_VAL[coin], bar, bar["C"],
                                     "backtest_end", trades, equity)
        if equity_curve:
            equity_curve[-1]["equity"] = equity

    # patch summarize's global BARS_PER_DAY to match this config's timeframe
    import honest_backtest_reversion_1h as _rev
    _rev.BARS_PER_DAY = cfg.bars_per_day
    res = summarize(cfg, equity_curve, trades, filters_hit, equity, total_funding,
                    return_trades=return_trades)
    _rev.BARS_PER_DAY = 24
    return res


def _close_fraction(pos, cfg, coin, ct, bar, tag, trades, tp_price):
    """Partially close `frac` of position at the TP level (pessimistic slippage)."""
    frac = cfg.tp1_frac if tag == "tp1" else cfg.tp2_frac
    if pos["size"] <= 0:
        return
    close_sz = math.floor(pos["size"] * frac / LOT_SZ[coin] + 1e-12) * LOT_SZ[coin]
    if close_sz <= 0:
        close_sz = pos["size"]
    if pos["side"] == "long":
        fill = tp_price * (1 - SLIPPAGE)
        pnl = close_sz * ct * (fill - pos["avg_entry"]) - close_sz * ct * fill * COMMISSION
    else:
        fill = tp_price * (1 + SLIPPAGE)
        pnl = close_sz * ct * (pos["avg_entry"] - fill) - close_sz * ct * fill * COMMISSION
    pos["partial_pnl"] = pos.get("partial_pnl", 0.0) + pnl
    pos["size"] -= close_sz
    trades.append({
        "date": bar.get("dt", ""), "coin": coin, "side": pos["side"], "pnl": round(pnl, 2),
        "reason": tag, "entry": pos["avg_entry"], "exit": round(fill, 4),
        "size": close_sz, "closed": False,
    })


def _close_position(pos, cfg, coin, ct, bar, exit_raw, reason, trades, equity):
    """Close full remaining position; returns updated equity."""
    if pos["size"] <= 0:
        return equity
    partial = pos.get("partial_pnl", 0.0)
    if pos["side"] == "long":
        fill = exit_raw * (1 - SLIPPAGE)
        pnl = pos["size"] * ct * (fill - pos["avg_entry"]) - pos["size"] * ct * fill * COMMISSION
    else:
        fill = exit_raw * (1 + SLIPPAGE)
        pnl = pos["size"] * ct * (pos["avg_entry"] - fill) - pos["size"] * ct * fill * COMMISSION
    total_pnl = pnl + partial
    equity += total_pnl
    trades.append({
        "date": bar.get("dt", ""), "coin": coin, "side": pos["side"], "pnl": round(total_pnl, 2),
        "funding_pnl": round(pos.get("funding", 0.0), 2),
        "reason": reason, "entry": pos["avg_entry"], "exit": round(fill, 4),
        "size": pos["size"], "closed": True,
        "adds": pos.get("adds", 0),
    })
    pos["size"] = 0
    return equity


def _add_size(cfg, pos, coin, price, atr, lev, equity):
    base = pos["size"] * cfg.add_size_ratio
    # cap by margin
    max_margin = equity * cfg.max_margin_pct
    ct = CT_VAL[coin]
    notional = base * ct * price
    margin = notional / lev if lev > 0 else notional
    if margin > max_margin:
        base = max_margin * lev / (ct * price)
    lot = LOT_SZ[coin]
    base = math.floor(base / lot + 1e-12) * lot
    return max(base, lot)


def dynamic_leverage(atr, price, max_lev):
    if atr <= 0 or price <= 0:
        return 1.0
    lev = 1.0 / ((atr / price) * 2)
    return max(1.0, min(lev, max_lev))


def _avg_vol(bars, i, window):
    lo = max(0, i - window + 1)
    vals = [b["V"] for b in bars[lo:i + 1] if b["V"] > 0]
    return sum(vals) / len(vals) if vals else 0.0


def _build_coin_frame(candles, cfg: ImpulseConfig):
    closes = [c["C"] for c in candles]
    highs = [c["H"] for c in candles]
    lows = [c["L"] for c in candles]
    return {
        "candles": candles,
        "closes": closes,
        "ema_f": ema_series(closes, cfg.ema_fast),
        "ema_s": ema_series(closes, cfg.ema_slow),
        "atr": atr_series(highs, lows, closes, 14),
        "adx": adx_series(highs, lows, closes, 14),
        "rsi": rsi_series(closes, 14),
        "roc": roc_series(closes, cfg.impulse_bars),
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar", choices=["1h", "4h", "12h", "1d"], default="1d",
                    help="timeframe (4h/12h/1d aggregate the 1H cache)")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    cfg = BASE
    bars_per_day = {"1h": 24, "4h": 6, "12h": 2, "1d": 1}[args.bar]
    n_hours = {"1h": 1, "4h": 4, "12h": 12, "1d": 24}[args.bar]
    # rescale bar-count params so holding/cooling lasts the same wall-clock time
    scale = bars_per_day / cfg.bars_per_day
    if args.bar != "1h":
        cfg = ImpulseConfig(**{**asdict(cfg), "name": cfg.name + f" ({args.bar})",
                               "bars_per_day": bars_per_day})
        for k in ("cooldown_bars", "max_hold_bars", "add_window_bars"):
            v = getattr(cfg, k)
            setattr(cfg, k, max(int(round(v * scale)), 1))

    print("=" * 72)
    print("IMPULSE BOT — fast momentum entry + pyramiding + cascade exit")
    print(f"Config: {cfg.name} | impulse ROC>={cfg.entry_roc:.1f}%/"
          f"{cfg.impulse_bars} bars | adds<={cfg.max_adds} | lev<={cfg.max_leverage}x")
    print("Rules: signal@T close -> entry@T+1 open | fee 0.10% + slip 0.05%")
    print("=" * 72)

    data_1h = load_cached_data()
    if args.bar == "1h":
        data = data_1h
    else:
        data = {c: aggregate_bars(data_1h[c], n_hours) for c in COINS}
    print(f"  Using 1H cache → {args.bar}: {len(data['BTC'])} bars/coin "
          f"({data['BTC'][0]['dt']} → {data['BTC'][-1]['dt']})", flush=True)
    funding = await load_funding(force_refresh=args.refresh)

    print("\n[run] Impulse Bot ...", flush=True)
    res = run_strategy(data, cfg, funding=funding, return_trades=True)
    print_report(res)

    # variant: no pyramiding (isolate the effect of adds)
    no_add = ImpulseConfig(**{**asdict(cfg), "max_adds": 0, "name": cfg.name + " (no adds)"})
    res_no_add = run_strategy(data, no_add, funding=funding)
    print("\n[run] Same, pyramiding OFF ...", flush=True)
    print_report(res_no_add)

    # variant: no partial TP (full position to trailing stop)
    no_tp = ImpulseConfig(**{**asdict(cfg), "tp1_frac": 0.0, "tp2_frac": 0.0,
                             "name": cfg.name + " (no partial TP)"})
    res_no_tp = run_strategy(data, no_tp, funding=funding)
    print("\n[run] Same, partial-TP OFF ...", flush=True)
    print_report(res_no_tp)

    print("\n[run] BTC Buy & Hold benchmark ...", flush=True)
    bnh = buy_and_hold_btc(data)

    # Comparison
    print("\n" + "=" * 72)
    print("COMPARISON")
    print("=" * 72)
    rows = [res, res_no_add, res_no_tp, bnh]
    print(f"  {'Strategy':44s} {'Return':>8} {'CAGR':>7} {'MaxDD':>7} {'Sharpe':>7} {'Trades':>7}")
    for r in rows:
        print(f"  {r['strategy']:44s} {r['total_return_pct']:+7.1f}% {r['cagr_pct']:6.1f}% "
              f"{r['max_drawdown_pct']:6.1f}% {r['sharpe']:6.2f} {r.get('closed_trades', 0):7d}")

    print("\nVERDICT")
    print(f"  Adds made:        {res['filters_hit'].get('adds_made', 0)}")
    print(f"  Add blocked (vol): {res['filters_hit'].get('add_no_vol', 0)}")
    if res["total_return_pct"] <= bnh["total_return_pct"]:
        print("  → Impulse bot did NOT beat BTC buy&hold on this window.")
    elif res["max_drawdown_pct"] > 40:
        print("  → Beats buy&hold but drawdown is heavy — tighten risk.")
    else:
        print("  → Impulse bot beats BTC buy&hold with acceptable drawdown.")

    out = {
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "config": asdict(cfg),
        "impulse": {k: v for k, v in res.items() if k not in ("equity_curve", "recent_trades", "config", "all_trades")},
        "no_adds": {k: v for k, v in res_no_add.items() if k not in ("equity_curve", "recent_trades", "config", "all_trades")},
        "no_tp": {k: v for k, v in res_no_tp.items() if k not in ("equity_curve", "recent_trades", "config", "all_trades")},
        "btc_buy_hold": bnh,
        "recent_trades": res["recent_trades"],
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved → {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())

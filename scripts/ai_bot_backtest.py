#!/usr/bin/env python3
"""AI Momentum Bot — walk-forward ML gate on top of Momentum Rotation.

Standalone experimental bot. Reuses the honest engine's rules and costs
(no look-ahead, signal@T close -> entry@T+1 open, commission 0.10% +
slippage 0.05%, pessimistic H/L stops, OKX SWAP daily data) but adds a
machine-learning layer that decides whether a momentum candidate is
worth taking:

  * Features (causal, at signal bar T): ROC, ROC-slow, EMA trend gap,
    ADX, RSI, ATR%, volatility ratio, BTC-regime, 20d realized vol,
    correlation vs BTC, momentum score, distance from SMA200.
  * Label: is close[T+N] higher than close[T] (long profitability).
  * Model: GradientBoosting classifier.
  * Walk-forward (NO look-ahead): model retrained every `retrain_every`
    days on a rolling window ending `gap` bars BEFORE the signal bar.
  * Gate: a long candidate is taken only if P(up) >= p_thresh; a short
    only if P(up) <= 1 - p_thresh. Otherwise the slot stays empty.

Run (needs sklearn, e.g. the freqtrade venv):

  python scripts/ai_bot_backtest.py                # ML gate vs baseline
  python scripts/ai_bot_backtest.py --thresh 0.55  # tighter gate
  python scripts/ai_bot_backtest.py --nohold       # no position hold (pure rotation)

Compares ML-gated vs ungated momentum (same engine) vs BTC buy&hold.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict
from dataclasses import asdict

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from honest_backtest_3y import (  # noqa: E402
    COINS,
    COMMISSION,
    SLIPPAGE,
    CT_VAL,
    LOT_SZ,
    StratConfig,
    V3_LIVE,
    V3_PROPOSED,
    _unrealized,
    adx_series,
    atr_series,
    buy_and_hold_btc,
    calc_size,
    correlation,
    dynamic_leverage,
    ema_series,
    load_data,
    load_funding,
    print_report,
    roc_series,
    rsi_series,
    sma_series,
    summarize,
)

from sklearn.ensemble import GradientBoostingClassifier  # noqa: E402

OUT_PATH = os.path.join(os.path.dirname(__file__), "ai_bot_results.json")

# ── ML hyper-parameters ──
HORIZON = 10          # predict if close is higher N bars later
GAP = 40              # bars between last training sample and signal bar (no leakage)
MIN_TRAIN = 400       # min samples before the first prediction is emitted
RETRAIN_EVERY = 90    # retrain cadence (days)
P_THRESH = 0.55       # default gate


# ── Feature builder (causal: value at i uses only data[:i+1]) ──

def build_features(daily_data: dict, cfg: StratConfig, oi_data: dict | None = None) -> dict:
    """Return {coin: {dates, X}} where X[j] is a numpy vector of features
    for the candle at index j (aligned to coin's own candle list).

    When oi_data (from fetch_oi) is provided, OI-based positioning
    features are appended (OI change 1d/5d/20d, OI vs its 30d mean,
    log OI in USD).
    """
    feat = {}
    for coin in COINS:
        bars = daily_data.get(coin, [])
        closes = [c["C"] for c in bars]
        highs = [c["H"] for c in bars]
        lows = [c["L"] for c in bars]
        n = len(closes)

        # OI aligned by date, if available
        oi_map = {}
        oi_delta_map = {}
        oi_usd_map = {}
        if oi_data and coin in oi_data:
            for r in oi_data[coin]:
                d = time.strftime("%Y-%m-%d", time.gmtime(int(r["ts"]) / 1000))
                oi_map[d] = float(r["oiCcy"])
                oi_delta_map[d] = float(r.get("oiDeltaPct", 0.0)) / 100.0
                oi_usd_map[d] = float(r.get("oiUsd", 0.0))
        oi_series = [oi_map.get(b["date"], 0.0) for b in bars]
        oi_delta_series = [oi_delta_map.get(b["date"], 0.0) for b in bars]
        oi_usd_series = [oi_usd_map.get(b["date"], 0.0) for b in bars]

        ema_f = ema_series(closes, cfg.ema_fast)
        ema_s = ema_series(closes, cfg.ema_slow)
        atr = atr_series(highs, lows, closes, cfg.atr_period)
        adx = adx_series(highs, lows, closes, 14)
        rsi = rsi_series(closes, 14)
        sma200 = sma_series(closes, 200)
        roc = roc_series(closes, cfg.roc_fast_period if cfg.roc_fast_period > 0 else cfg.roc_period)
        roc_slow = roc_series(closes, cfg.roc_slow_period) if cfg.roc_fast_period > 0 else None

        btc_bars = daily_data["BTC"]
        btc_closes = [c["C"] for c in btc_bars]
        btc_sma200 = sma_series(btc_closes, 200)
        btc_returns = [btc_closes[j] / btc_closes[j - 1] - 1 if j > 0 and btc_closes[j - 1] > 0 else 0.0
                       for j in range(len(btc_closes))]

        def btc_regime_at(date_idx):
            if btc_sma200[date_idx] > 0:
                return 1.0 if btc_closes[date_idx] > btc_sma200[date_idx] else 0.0
            return 0.0

        rows = []
        for i in range(n):
            c = closes[i]
            v = [
                roc[i] / 100.0,                                    # ROC (fast)
                (roc_slow[i] / 100.0 if roc_slow else roc[i] / 100.0),  # ROC slow
                (ema_f[i] - ema_s[i]) / ema_s[i] if ema_s[i] > 0 else 0.0,  # trend gap
                adx[i] / 100.0,                                    # ADX
                rsi[i] / 100.0,                                    # RSI
                atr[i] / c if c > 0 else 0.0,                      # ATR%
                # 20d realized vol
                _realized_vol(closes, i, 20),
                # ATR vs its 30d average (volatility regime)
                _vol_ratio(atr, i, 30),
                # distance from SMA200
                (c / sma200[i] - 1.0) if sma200[i] > 0 else 0.0,
                # momentum score used by the rule-based engine
                _mom_score(roc[i], (ema_f[i] - ema_s[i]) / ema_s[i] if ema_s[i] > 0 else 0.0, adx[i]),
                # correlation vs BTC over 30d
                _corr_btc(closes, btc_returns, i, 30),
                btc_regime_at(i),                                  # BTC regime
                closes[i - 1] / c if c > 0 and i > 0 else 0.0,      # prior-day sign of move
                # ── OI-based positioning features (causal) ──
                _oi_change(oi_series, i, 1),
                _oi_change(oi_series, i, 5),
                _oi_change(oi_series, i, 20),
                _oi_ratio_vs_mean(oi_series, i, 30),
                _log_oi_usd(oi_usd_series, i),
            ]
            rows.append(np.array(v, dtype=float))
        feat[coin] = {"dates": [b["date"] for b in bars], "X": rows}
    return feat


def _oi_change(oi, i, window):
    """OI change over `window` bars: oi[i]/oi[i-window] - 1. 0 if missing."""
    if i < window or oi[i] <= 0 or oi[i - window] <= 0:
        return 0.0
    return oi[i] / oi[i - window] - 1.0


def _oi_ratio_vs_mean(oi, i, window=30):
    """Current OI / mean of last `window` OI values. 0 if not enough data."""
    lo = max(0, i - window + 1)
    vals = [v for v in oi[lo:i + 1] if v > 0]
    if len(vals) < 10 or oi[i] <= 0:
        return 0.0
    avg = sum(vals) / len(vals)
    return oi[i] / avg - 1.0 if avg > 0 else 0.0


def _log_oi_usd(oi_usd, i):
    """log10 of OI in USD, normalized to ~0-1 range. 0 if missing."""
    if oi_usd[i] <= 0:
        return 0.0
    return math.log10(oi_usd[i]) / 13.0


def _realized_vol(closes, i, window=20):
    if i < window + 1:
        return 0.0
    rets = [math.log(closes[j] / closes[j - 1]) for j in range(i - window + 1, i + 1)
            if closes[j - 1] > 0]
    if len(rets) < 5:
        return 0.0
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / len(rets)
    return math.sqrt(var)


def _vol_ratio(atr, i, window=30):
    lo = max(0, i - window + 1)
    vals = [a for a in atr[lo:i + 1] if a > 0]
    if not vals:
        return 0.0
    avg = sum(vals) / len(vals)
    return atr[i] / avg if avg > 0 else 0.0


def _mom_score(roc, trend, adx):
    return roc * 0.5 + trend * 0.3 + (adx / 50.0) * 0.2


def _corr_btc(closes, btc_returns, i, window=30):
    lo = max(1, i - window + 1)
    a, b = [], []
    for j in range(lo, i + 1):
        if closes[j - 1] > 0:
            a.append(closes[j] / closes[j - 1] - 1)
            b.append(btc_returns[j] if j < len(btc_returns) else 0.0)
    if len(a) < 10:
        return 0.0
    return correlation(a, b, len(a))


def _labels_for(daily_data, cfg):
    """{coin: {date: 1 if close[date+HORIZON] > close[date] else 0}}."""
    labels = {}
    for coin in COINS:
        bars = daily_data.get(coin, [])
        closes = [c["C"] for c in bars]
        dates = [c["date"] for c in bars]
        d = {}
        for j in range(len(bars) - HORIZON):
            d[dates[j]] = 1.0 if closes[j + HORIZON] > closes[j] else 0.0
        labels[coin] = d
    return labels


# ── Walk-forward predictions ──

def walk_forward_predictions(feat: dict, labels: dict, p_thresh=P_THRESH,
                             min_train=MIN_TRAIN, retrain_every=RETRAIN_EVERY,
                             gap=GAP):
    """Return {date: {coin: p_up}} for every date where a valid model exists.

    Model is trained only on samples whose signal bar is <= date - gap,
    so the 10-day label window never overlaps the prediction point.
    """
    # Collect (signal_date, coin, X, y) samples, causal, in time order.
    samples = []
    for coin, info in feat.items():
        dates = info["dates"]
        X = info["X"]
        lbl = labels[coin]
        for j in range(len(X)):
            d = dates[j]
            if d in lbl:
                samples.append((d, coin, X[j], lbl[d]))
    samples.sort(key=lambda s: s[0])

    # Unique signal dates in order (global, across coins).
    sig_dates = sorted({s[0] for s in samples})
    date_pos = {d: i for i, d in enumerate(sig_dates)}

    probs = {}
    model = None
    last_train_date = None
    n_train = 0

    for d in sig_dates:
        pos = date_pos[d]
        # retrain when we reach a date >= last_train + retrain_every
        should_train = False
        if model is None:
            should_train = True
        elif last_train_date is not None:
            if pos - date_pos[last_train_date] >= retrain_every:
                should_train = True

        if should_train:
            cutoff_date = sig_dates[max(0, pos - gap)]
            # samples with signal date <= cutoff_date are trainable
            X_tr, y_tr = [], []
            for s in samples:
                if date_pos[s[0]] <= date_pos[cutoff_date]:
                    X_tr.append(s[2])
                    y_tr.append(s[3])
                else:
                    break
            n_train = len(X_tr)
            if n_train >= min_train:
                model = GradientBoostingClassifier(
                    n_estimators=120, learning_rate=0.05, max_depth=3,
                    subsample=0.8, random_state=42)
                model.fit(np.array(X_tr), np.array(y_tr))
                last_train_date = d
            else:
                model = None

        if model is None:
            continue
        # predict for all coins at this signal date
        day_probs = {}
        for coin in COINS:
            info = feat[coin]
            dates = info["dates"]
            if d in dates:
                j = dates.index(d)
                p = model.predict_proba(info["X"][j].reshape(1, -1))[0][1]
                day_probs[coin] = float(p)
        probs[d] = day_probs
    return probs


# ── Engine (copy of honest engine's rules with an ML gate) ──

def run_strategy_ml(daily_data: dict, cfg: StratConfig, probs: dict,
                    p_thresh=P_THRESH, funding: dict | None = None,
                    gate=True) -> dict:
    """Momentum rotation with an optional ML probability gate.

    Candidates are generated by the exact same rules/filters as the
    honest engine; when `gate` is True a long candidate is taken only if
    probs[sig_date][coin] >= p_thresh (short: p <= 1 - p_thresh).
    """
    coin_data = {}
    for coin in COINS:
        bars = daily_data.get(coin, [])
        if len(bars) < 250:
            raise RuntimeError(f"{coin}: not enough bars ({len(bars)})")
        coin_data[coin] = _build_coin_frame(bars, cfg)

    common = None
    for coin, cd in coin_data.items():
        dates = {c["date"] for c in cd["candles"]}
        common = dates if common is None else (common & dates)
    all_dates = sorted(common)

    date_idx = {
        coin: {c["date"]: i for i, c in enumerate(cd["candles"])}
        for coin, cd in coin_data.items()
    }

    equity = cfg.capital
    positions = {}
    trades = []
    equity_curve = []
    filters_hit = defaultdict(int)
    last_rotate = -10 ** 9
    start_i = max(cfg.ema_slow + 20, 210)
    total_funding = 0.0

    for i in range(start_i, len(all_dates)):
        date = all_dates[i]
        sig_date = all_dates[i - 1]

        mtm = {}
        for coin in COINS:
            ci = date_idx[coin][date]
            mtm[coin] = coin_data[coin]["candles"][ci]["C"]

        # ── 0. Funding accrual ──
        if funding:
            for coin in list(positions.keys()):
                pos = positions[coin]
                ci = date_idx[coin][date]
                bar = coin_data[coin]["candles"][ci]
                rate_day = funding.get(coin, {}).get(date, 0.0)
                if rate_day != 0.0:
                    notional = pos["size"] * CT_VAL[coin] * bar["C"]
                    fpnl = -notional * rate_day if pos["side"] == "long" else notional * rate_day
                    equity += fpnl
                    total_funding += fpnl
                    pos["funding"] = pos.get("funding", 0.0) + fpnl

        # ── 1. Manage open positions on TODAY's bar ──
        for coin in list(positions.keys()):
            pos = positions[coin]
            ci = date_idx[coin][date]
            bar = coin_data[coin]["candles"][ci]
            ct = CT_VAL[coin]
            trail = pos["atr"] * cfg.trail_atr_mult
            if trail <= 0:
                trail = pos["entry"] * 0.02

            hit_stop = False
            hit_partial = False
            exit_raw = None
            reason = "trail_stop"

            if pos["side"] == "long":
                if bar["L"] <= pos["stop"]:
                    hit_stop = True
                    exit_raw = pos["stop"]
                if not hit_stop:
                    if bar["H"] > pos["peak"]:
                        pos["peak"] = bar["H"]
                        new_stop = pos["peak"] - trail
                        if new_stop > pos["stop"]:
                            pos["stop"] = new_stop
                    if (not pos["breakeven"]
                            and bar["C"] >= pos["entry"] * (1 + cfg.breakeven_pct)):
                        pos["stop"] = max(pos["stop"], pos["entry"] * 0.999)
                        pos["breakeven"] = True
                    if (not pos["partial"]
                            and bar["H"] >= pos["entry"] * (1 + cfg.partial_tp_pct)):
                        hit_partial = True
                        exit_raw = pos["entry"] * (1 + cfg.partial_tp_pct)
            else:
                if bar["H"] >= pos["stop"]:
                    hit_stop = True
                    exit_raw = pos["stop"]
                if not hit_stop:
                    if bar["L"] < pos["peak"]:
                        pos["peak"] = bar["L"]
                        new_stop = pos["peak"] + trail
                        if new_stop < pos["stop"]:
                            pos["stop"] = new_stop
                    if (not pos["breakeven"]
                            and bar["C"] <= pos["entry"] * (1 - cfg.breakeven_pct)):
                        pos["stop"] = min(pos["stop"], pos["entry"] * 1.001)
                        pos["breakeven"] = True
                    if (not pos["partial"]
                            and bar["L"] <= pos["entry"] * (1 - cfg.partial_tp_pct)):
                        hit_partial = True
                        exit_raw = pos["entry"] * (1 - cfg.partial_tp_pct)

            if hit_partial and not hit_stop:
                close_sz = math.floor(pos["size"] * cfg.partial_tp_ratio / LOT_SZ[coin] + 1e-12) * LOT_SZ[coin]
                if close_sz > 0 and close_sz < pos["size"]:
                    if pos["side"] == "long":
                        fill = exit_raw * (1 - SLIPPAGE)
                        pnl = close_sz * ct * (fill - pos["entry"]) - close_sz * ct * fill * COMMISSION
                    else:
                        fill = exit_raw * (1 + SLIPPAGE)
                        pnl = close_sz * ct * (pos["entry"] - fill) - close_sz * ct * fill * COMMISSION
                    equity += pnl
                    trades.append({
                        "date": date, "coin": coin, "side": pos["side"], "pnl": round(pnl, 2),
                        "reason": "partial_tp", "entry": pos["entry"], "exit": round(fill, 4),
                        "size": close_sz, "closed": False,
                    })
                    pos["size"] -= close_sz
                    pos["partial"] = True

            if hit_stop:
                if pos["side"] == "long":
                    fill = exit_raw * (1 - SLIPPAGE)
                    pnl = pos["size"] * ct * (fill - pos["entry"]) - pos["size"] * ct * fill * COMMISSION
                else:
                    fill = exit_raw * (1 + SLIPPAGE)
                    pnl = pos["size"] * ct * (pos["entry"] - fill) - pos["size"] * ct * fill * COMMISSION
                equity += pnl
                trades.append({
                    "date": date, "coin": coin, "side": pos["side"], "pnl": round(pnl, 2),
                    "funding_pnl": round(pos.get("funding", 0.0), 2),
                    "reason": reason, "entry": pos["entry"], "exit": round(fill, 4),
                    "size": pos["size"], "closed": True, "hold_days": i - pos["entry_i"],
                })
                del positions[coin]

        # ── 2. Rotation using YESTERDAY signal only ──
        if i - last_rotate < cfg.min_hold_days and positions:
            unreal = _unrealized(positions, mtm)
            equity_curve.append({"date": date, "equity": equity + unreal})
            continue

        btc_si = date_idx["BTC"][sig_date]
        btc_cd = coin_data["BTC"]
        btc_above = True
        if btc_cd["sma200"][btc_si] > 0:
            btc_above = btc_cd["closes"][btc_si] > btc_cd["sma200"][btc_si]

        ranked = []
        for coin, cd in coin_data.items():
            si = date_idx[coin][sig_date]
            atr = cd["atr"][si]
            if atr <= 0:
                continue
            atr_slice = [cd["atr"][j] for j in range(max(0, si - 29), si + 1) if cd["atr"][j] > 0]
            avg_atr = sum(atr_slice) / len(atr_slice) if atr_slice else 0.0
            if avg_atr > 0 and atr > avg_atr * cfg.vol_mult:
                filters_hit["volatility"] += 1
                continue

            ema_trend = cd["ema_f"][si] > cd["ema_s"][si]
            rsi = cd["rsi"][si]
            roc = cd["roc"][si]
            roc_slow = cd.get("roc_slow", [0.0])[si] if cfg.roc_fast_period > 0 else roc
            adx = cd["adx"][si]

            if cfg.min_roc > 0 and abs(roc) < cfg.min_roc:
                filters_hit["min_roc"] += 1
                continue
            if rsi > cfg.rsi_long_max and ema_trend:
                filters_hit["rsi_overbought"] += 1
                continue
            if rsi < cfg.rsi_short_min and not ema_trend:
                filters_hit["rsi_oversold"] += 1
                continue
            if not btc_above and roc > 0 and ema_trend:
                filters_hit["bear_long"] += 1
                continue

            trend_val = 0.0
            if cd["ema_s"][si] > 0:
                trend_val = (cd["ema_f"][si] - cd["ema_s"][si]) / cd["ema_s"][si] * 100
            score = roc * 0.5 + trend_val * 0.3 + (adx / 50) * 0.2

            rets = []
            for j in range(max(1, si - 29), si + 1):
                if cd["closes"][j - 1] > 0:
                    rets.append(cd["closes"][j] / cd["closes"][j - 1] - 1)

            ranked.append({
                "coin": coin, "score": score, "roc": roc, "roc_slow": roc_slow,
                "ema_trend": ema_trend, "adx": adx, "atr": atr, "rets": rets,
            })

        ranked.sort(key=lambda x: x["score"], reverse=True)

        day_probs = probs.get(sig_date, {})
        targets = []
        for row in ranked:
            if len(targets) >= cfg.top_k:
                break
            slow_ok = cfg.roc_fast_period <= 0 or row["roc_slow"] > 0
            if row["roc"] > 0 and row["ema_trend"] and slow_ok and row["adx"] >= cfg.adx_min:
                side = "long"
            elif (cfg.allow_short and row["roc"] < 0 and not row["ema_trend"]
                  and (cfg.roc_fast_period <= 0 or row["roc_slow"] < 0)
                  and row["adx"] >= cfg.adx_min):
                side = "short"
            else:
                continue

            # ── ML gate ──
            if gate:
                p = day_probs.get(row["coin"])
                if p is None:
                    filters_hit["ml_no_model"] += 1
                    continue
                if side == "long" and p < p_thresh:
                    filters_hit["ml_reject_long"] += 1
                    continue
                if side == "short" and p > (1 - p_thresh):
                    filters_hit["ml_reject_short"] += 1
                    continue

            corr_ok = True
            check_against = [positions[c]["rets"] for c in positions] + [t["rets"] for t in targets]
            for held_rets in check_against:
                if abs(correlation(row["rets"], held_rets)) > cfg.corr_threshold:
                    filters_hit["correlation"] += 1
                    corr_ok = False
                    break
            if not corr_ok:
                continue
            targets.append({"coin": row["coin"], "side": side, "atr": row["atr"], "rets": row["rets"]})

        target_set = {(t["coin"], t["side"]) for t in targets}

        for coin in list(positions.keys()):
            pos = positions[coin]
            if (coin, pos["side"]) in target_set:
                continue
            ci = date_idx[coin][date]
            exit_raw = coin_data[coin]["candles"][ci]["O"]
            ct = CT_VAL[coin]
            if pos["side"] == "long":
                fill = exit_raw * (1 - SLIPPAGE)
                pnl = pos["size"] * ct * (fill - pos["entry"]) - pos["size"] * ct * fill * COMMISSION
            else:
                fill = exit_raw * (1 + SLIPPAGE)
                pnl = pos["size"] * ct * (pos["entry"] - fill) - pos["size"] * ct * fill * COMMISSION
            equity += pnl
            trades.append({
                "date": date, "coin": coin, "side": pos["side"], "pnl": round(pnl, 2),
                "funding_pnl": round(pos.get("funding", 0.0), 2),
                "reason": "rotation_exit", "entry": pos["entry"], "exit": round(fill, 4),
                "size": pos["size"], "closed": True, "hold_days": i - pos["entry_i"],
            })
            del positions[coin]

        for t in targets:
            coin, side = t["coin"], t["side"]
            if coin in positions:
                continue
            ci = date_idx[coin][date]
            entry_raw = coin_data[coin]["candles"][ci]["O"]
            atr = t["atr"]
            lev = dynamic_leverage(atr, entry_raw, cfg.max_leverage)
            stop_dist = atr * cfg.atr_stop_mult
            if side == "long":
                fill = entry_raw * (1 + SLIPPAGE)
                stop = fill - stop_dist
            else:
                fill = entry_raw * (1 - SLIPPAGE)
                stop = fill + stop_dist

            sz = calc_size(equity, coin, fill, stop_dist, lev, cfg.risk_per_trade, cfg.max_margin_pct)
            fee = sz * CT_VAL[coin] * fill * COMMISSION
            equity -= fee
            positions[coin] = {
                "side": side, "size": sz, "entry": fill, "stop": stop,
                "peak": fill, "atr": atr, "lev": lev, "breakeven": False,
                "partial": False, "entry_i": i, "rets": t["rets"],
            }
            trades.append({
                "date": date, "coin": coin, "side": side, "pnl": round(-fee, 2),
                "reason": "open", "entry": round(fill, 4), "exit": None,
                "size": sz, "closed": False, "leverage": round(lev, 2),
            })
            last_rotate = i

        unreal = _unrealized(positions, mtm)
        equity_curve.append({"date": date, "equity": equity + unreal})

    # Force-close at last close
    if positions and all_dates:
        date = all_dates[-1]
        for coin in list(positions.keys()):
            pos = positions[coin]
            ci = date_idx[coin][date]
            exit_raw = coin_data[coin]["candles"][ci]["C"]
            ct = CT_VAL[coin]
            if pos["side"] == "long":
                fill = exit_raw * (1 - SLIPPAGE)
                pnl = pos["size"] * ct * (fill - pos["entry"]) - pos["size"] * ct * fill * COMMISSION
            else:
                fill = exit_raw * (1 + SLIPPAGE)
                pnl = pos["size"] * ct * (pos["entry"] - fill) - pos["size"] * ct * fill * COMMISSION
            equity += pnl
            trades.append({
                "date": date, "coin": coin, "side": pos["side"], "pnl": round(pnl, 2),
                "funding_pnl": round(pos.get("funding", 0.0), 2),
                "reason": "backtest_end", "entry": pos["entry"], "exit": round(fill, 4),
                "size": pos["size"], "closed": True,
            })
        if equity_curve:
            equity_curve[-1]["equity"] = equity

    return summarize(cfg, equity_curve, trades, filters_hit, equity, total_funding)


def _build_coin_frame(candles, cfg: StratConfig):
    closes = [c["C"] for c in candles]
    highs = [c["H"] for c in candles]
    lows = [c["L"] for c in candles]
    frame = {
        "candles": candles,
        "closes": closes,
        "ema_f": ema_series(closes, cfg.ema_fast),
        "ema_s": ema_series(closes, cfg.ema_slow),
        "atr": atr_series(highs, lows, closes, cfg.atr_period),
        "adx": adx_series(highs, lows, closes, 14),
        "rsi": rsi_series(closes, 14),
        "sma200": sma_series(closes, 200),
    }
    if cfg.roc_fast_period > 0:
        frame["roc"] = roc_series(closes, cfg.roc_fast_period)
        frame["roc_slow"] = roc_series(closes, cfg.roc_slow_period)
    else:
        frame["roc"] = roc_series(closes, cfg.roc_period)
    return frame


def _model_quality(probs: dict, labels: dict, dates: list) -> dict:
    """OOS AUC + accuracy + coverage using walk-forward probabilities."""
    y_true, y_score, n_covered = [], [], 0
    for d in dates:
        if d not in probs:
            continue
        for coin in COINS:
            if d in labels[coin]:
                n_covered += 1
                y_true.append(labels[coin][d])
                y_score.append(probs[d][coin])
    if len(y_true) < 50:
        return {"n": len(y_true)}
    from sklearn.metrics import roc_auc_score
    auc = roc_auc_score(y_true, y_score)
    preds = [1 if p >= 0.5 else 0 for p in y_score]
    acc = sum(1 for t, p in zip(y_true, preds) if t == p) / len(y_true)
    return {"n": len(y_true), "auc": round(auc, 3), "accuracy": round(acc, 3),
            "coverage_pct": round(n_covered / (len(dates) * len(COINS)) * 100, 1)}


FEATURE_NAMES = [
    "ROC_fast", "ROC_slow", "EMA_gap", "ADX", "RSI", "ATR%", "realvol20",
    "vol_ratio", "SMA200_dist", "mom_score", "corr_BTC", "BTC_regime",
    "prev_move", "OI_chg_1d", "OI_chg_5d", "OI_chg_20d", "OI_vs_mean30",
    "log_OI_usd",
]


def _feature_importance(feat: dict, labels: dict) -> dict:
    """Train one GB on the full dataset and report mean feature importance.
    Diagnostic only (uses the full window; not used for trading)."""
    X, y = [], []
    for coin, info in feat.items():
        dates = info["dates"]
        lbl = labels[coin]
        for j in range(len(dates)):
            d = dates[j]
            if d in lbl:
                X.append(info["X"][j])
                y.append(lbl[d])
    if len(X) < 100:
        return {}
    m = GradientBoostingClassifier(
        n_estimators=200, learning_rate=0.05, max_depth=3,
        subsample=0.8, random_state=42)
    m.fit(np.array(X), np.array(y))
    imp = m.feature_importances_
    names = FEATURE_NAMES[:len(imp)]
    pairs = sorted(zip(names, imp), key=lambda p: -p[1])
    return {n: round(v, 4) for n, v in pairs}, round(
        sum(v for n, v in pairs if n.startswith("OI")) / sum(p[1] for p in pairs) * 100, 1)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--thresh", type=float, default=P_THRESH)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-oi", action="store_true", help="exclude OI features (baseline ML)")
    ap.add_argument("--no-gate", action="store_true", help="run baseline momentum (no ML)")
    args = ap.parse_args()

    oi_data = None
    if not args.no_oi:
        from fetch_oi import CACHE_PATH as OI_CACHE_PATH
        if os.path.exists(OI_CACHE_PATH):
            with open(OI_CACHE_PATH) as f:
                cached = json.load(f)
            if all(c in cached.get("data", {}) for c in COINS):
                oi_data = cached["data"]
                print(f"  Using OI cache ({len(oi_data.get('BTC', []))} bars/coin)", flush=True)
            else:
                print("  WARN: OI cache incomplete, running without OI features", flush=True)
        else:
            print("  WARN: no OI cache found, run scripts/fetch_oi.py first", flush=True)

    print("=" * 72)
    print("AI MOMENTUM BOT — walk-forward ML gate on Momentum Rotation")
    feat_names = "ROC/EMA-gap/ADX/RSI/ATR%/vol/BTC-regime/corr/SMA200"
    if oi_data:
        feat_names += "/OI-change/OI-mean/log-OI"
    print(f"Features: {feat_names} | "
          f"horizon={HORIZON}d | gap={GAP} | retrain={RETRAIN_EVERY}d | gate>={args.thresh}")
    print("Rules: signal@T close -> entry@T+1 open | fee 0.10% + slip 0.05%")
    print("=" * 72)

    data = await load_data(force_refresh=args.refresh)
    funding = await load_funding(force_refresh=args.refresh)

    print("\n[1/4] Building causal features...", flush=True)
    feat = build_features(data, V3_LIVE, oi_data=oi_data)
    labels = _labels_for(data, V3_LIVE)
    all_dates = sorted({c["date"] for c in data["BTC"]})

    print("[2/4] Walk-forward training (GradientBoosting, no look-ahead)...", flush=True)
    t0 = time.time()
    probs = walk_forward_predictions(feat, labels, p_thresh=args.thresh)
    print(f"      done in {time.time() - t0:.1f}s, predictions on "
          f"{len(probs)} signal days", flush=True)

    q = _model_quality(probs, labels, all_dates)
    print(f"      model quality (OOS): AUC={q.get('auc', 'n/a')} "
          f"acc={q.get('accuracy', 'n/a')} n={q.get('n', 0)} "
          f"coverage={q.get('coverage_pct', 0)}%")

    imp, oi_share = _feature_importance(feat, labels)
    if imp:
        print("      feature importance (full-window GB, diagnostic):")
        for i, (n, v) in enumerate(imp.items()):
            if i >= 10:
                break
            print(f"        {n:14s} {v:.4f}")
        if oi_data:
            print(f"      OI features share of importance: {oi_share:.1f}%")

    print("\n[3/4] Running backtests...", flush=True)
    cfg = V3_LIVE
    base = run_strategy_ml(data, cfg, probs, p_thresh=args.thresh, funding=funding, gate=False)
    ml = run_strategy_ml(data, cfg, probs, p_thresh=args.thresh, funding=funding, gate=True)

    # Also the v4 proposed config through the gate
    cfg4 = V3_PROPOSED
    feat4 = build_features(data, cfg4, oi_data=oi_data)
    labels4 = _labels_for(data, cfg4)
    probs4 = walk_forward_predictions(feat4, labels4, p_thresh=args.thresh)
    q4 = _model_quality(probs4, labels4, all_dates)
    ml4 = run_strategy_ml(data, cfg4, probs4, p_thresh=args.thresh, funding=funding, gate=True)

    print("\n[4/4] BTC buy & hold benchmark...", flush=True)
    bnh = buy_and_hold_btc(data)

    print("\n" + "-" * 72)
    print("BASELINE (momentum, no ML gate)")
    print("-" * 72)
    print_report(base)
    print("\n" + "-" * 72)
    print(f"ML-GATED (gate>={args.thresh})")
    print("-" * 72)
    print_report(ml)
    print("\n" + "-" * 72)
    print(f"ML-GATED v4 config (gate>={args.thresh})  [OOS AUC {q4.get('auc', 'n/a')}]")
    print("-" * 72)
    print_report(ml4)

    # Comparison
    print("\n" + "=" * 72)
    print("COMPARISON")
    print("=" * 72)
    rows = [base, ml, ml4, bnh]
    print(f"  {'Strategy':30s} {'Return':>8} {'CAGR':>7} {'MaxDD':>7} {'Sharpe':>7} {'Trades':>7}")
    for r in rows:
        print(f"  {r['strategy']:30s} {r['total_return_pct']:+7.1f}% {r['cagr_pct']:6.1f}% "
              f"{r['max_drawdown_pct']:6.1f}% {r['sharpe']:6.2f} {r.get('closed_trades', 0):7d}")

    # Verdict
    print("\nVERDICT")
    if ml["total_return_pct"] <= base["total_return_pct"] * 0.95 and ml["max_drawdown_pct"] >= base["max_drawdown_pct"] * 0.95:
        print("  → ML gate does NOT clearly improve the momentum baseline on this window.")
    elif ml["max_drawdown_pct"] <= base["max_drawdown_pct"] * 0.85:
        print("  → ML gate meaningfully cuts drawdown (risk reduction).")
    else:
        print("  → ML gate changes returns but no clear dominance vs baseline.")
    print(f"  ML OOS AUC: {q.get('auc', 'n/a')} (0.50 = coin flip)")

    out = {
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "ml_params": {"horizon": HORIZON, "gap": GAP, "min_train": MIN_TRAIN,
                      "retrain_every": RETRAIN_EVERY, "p_thresh": args.thresh,
                      "use_oi": oi_data is not None,
                      "model": "GradientBoostingClassifier(n=120,depth=3,lr=0.05)"},
        "model_quality": q,
        "model_quality_v4": q4,
        "baseline": {k: v for k, v in base.items() if k not in ("equity_curve", "recent_trades", "config", "all_trades")},
        "ml_gated": {k: v for k, v in ml.items() if k not in ("equity_curve", "recent_trades", "config", "all_trades")},
        "ml_gated_v4": {k: v for k, v in ml4.items() if k not in ("equity_curve", "recent_trades", "config", "all_trades")},
        "btc_buy_hold": bnh,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved → {OUT_PATH}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

#!/usr/bin/env python3
"""Momentum Rotation v4 — Backtrader (bt) engine.

Independent re-implementation of the live `rotation_strategy.py` (v4) on top of
Backtrader's cerebro framework. Fetches real OKX daily candles, runs the rotation
logic as a bt.Strategy and reports metrics via bt built-in analyzers
(SharpeRatio, DrawDown, TradeAnalyzer) + broker value curve.

Modeling notes vs the pandas backtest engine:
  * Signals are computed on the PREVIOUS completed bar close (Backtrader line[-1]),
    all orders are market orders that fill at the NEXT bar open  -> matches the
    "signal on close, enter at open" rule of the live bot.
  * Entries/exits/rotation all fill at the next open. The pandas engine fills
    stops at the intrabar stop price; here stop/trail/partial/ROI exits are decided
    on the current bar H/L and the market order fills one bar later (conservative
    for gap-downs). Stop-loss levels therefore act as triggers, not guaranteed fills.
  * Commission 0.10%/side + slippage 0.05% are applied by the bt broker.
  * Sizing math is identical to the live bot (risk-based, leverage-capped,
    margin capped at `allocation_pct` of equity).

Usage:
  python backtrader_momentum_rotation.py                 # BTC/ETH/BNB/SOL, 1y, $10k
  python backtrader_momentum_rotation.py --pairs BTC,ETH,BNB,SOL,XRP,DOGE,ADA,TRX,AVAX,LTC --days 1100
"""

import argparse
import math

import backtrader as bt
import numpy as np
import bt_okx

# Backtrader broker requires cash >= notional even when the strategy uses
# leverage. We inflate cash by SCALE and keep sizes in real units, so leveraged
# exposure (notional up to ~2x equity) never trips the broker's margin check.
# All reported $ figures are divided back by SCALE.
SCALE = 100

# ── Defaults mirroring RotationConfig v4, tuned 2026-08 ─────────────────────
# Tuned config: risk=0.20, alloc=0.5, vol filter vol_mult=2.0 (improved vs 2.2 on same window),
# adx_min=25, looser corr filter (0.85), wider stop (4.5 ATR) + wider trailing
# (3 ATR), min_hold=11.
# Validated on OKX native 1D, 10 coins, 2023-05..2026-08:
#   CAGR ~60%, Sharpe 1.23, MaxDD ~-52%.
TOP_K = 2
ROC_PERIOD = 14
EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
ADX_MIN = 25.0
MIN_ROC = 4.5
SMA_LONG = 200
SMA_REGIME = 50
MIN_HOLD_DAYS = 11
MAX_LEVERAGE = 2.0
RISK_PER_TRADE = 0.20
ALLOCATION_PCT = 0.5
ATR_STOP_MULT = 4.5
TRAIL_ATR_MULT = 3.0
BREAKEVEN_PCT = 0.05
PARTIAL_TP_PCT = 0.06
PARTIAL_TP_RATIO = 0.25
PARTIAL_TP2_PCT = 0.12
PARTIAL_TP2_RATIO = 0.3
ROI_TABLE = [(17, 0.00), (8, 0.092), (3, 0.237), (0, 0.376)]
RSI_PERIOD = 14
RSI_LONG_MAX = 82.0
RSI_SHORT_MIN = 21.0
VOL_MULT = 2.0
CORR_THRESHOLD = 0.85
ALLOW_SHORT = True
COMMISSION = 0.001
SLIPPAGE = 0.0005

SWAP_MAP = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP", "BNB": "BNB-USDT-SWAP",
            "SOL": "SOL-USDT-SWAP", "XRP": "XRP-USDT-SWAP", "DOGE": "DOGE-USDT-SWAP",
            "ADA": "ADA-USDT-SWAP", "TRX": "TRX-USDT-SWAP", "AVAX": "AVAX-USDT-SWAP",
            "LTC": "LTC-USDT-SWAP"}


def roi_target(hold_bars: int) -> float:
    for min_hold, tp_pct in ROI_TABLE:
        if hold_bars >= min_hold:
            return tp_pct
    return 0.0


def _corr(a, b):
    a, b = list(a), list(b)
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    if n < 10:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if sa == 0 or sb == 0:
        return 0.0
    return cov / (sa * sb)


class MomentumRotationV4(bt.Strategy):
    """Live-faithful rotation: rank by score, regime on BTC SMA50/200."""

    params = (
        ("top_k", TOP_K), ("roc_period", ROC_PERIOD), ("ema_fast", EMA_FAST),
        ("ema_slow", EMA_SLOW), ("atr_period", ATR_PERIOD), ("adx_min", ADX_MIN),
        ("min_roc", MIN_ROC), ("sma_long", SMA_LONG), ("sma_regime", SMA_REGIME),
        ("min_hold_days", MIN_HOLD_DAYS), ("max_leverage", MAX_LEVERAGE),
        ("risk_per_trade", RISK_PER_TRADE), ("allocation_pct", ALLOCATION_PCT),
        ("atr_stop_mult", ATR_STOP_MULT), ("trail_atr_mult", TRAIL_ATR_MULT),
        ("breakeven_pct", BREAKEVEN_PCT), ("partial_tp_pct", PARTIAL_TP_PCT),
        ("partial_tp_ratio", PARTIAL_TP_RATIO),
        ("partial_tp2_pct", PARTIAL_TP2_PCT), ("partial_tp2_ratio", PARTIAL_TP2_RATIO),
        ("rsi_period", RSI_PERIOD),
        ("rsi_long_max", RSI_LONG_MAX), ("rsi_short_min", RSI_SHORT_MIN),
        ("vol_mult", VOL_MULT), ("corr_threshold", CORR_THRESHOLD),
        ("allow_short", ALLOW_SHORT), ("verbose", False),
    )

    def __init__(self):
        # Per-feed indicator lines (value at bar N lives in line[N]).
        self.emaf = [bt.indicators.EMA(d.close, period=self.p.ema_fast) for d in self.datas]
        self.emas = [bt.indicators.EMA(d.close, period=self.p.ema_slow) for d in self.datas]
        self.roc = [bt.indicators.RateOfChange(d.close, period=self.p.roc_period) for d in self.datas]
        self.adx = [bt.indicators.ADX(d, period=14) for d in self.datas]
        self.atr = [bt.indicators.ATR(d, period=self.p.atr_period) for d in self.datas]
        self.atr30 = [bt.indicators.SimpleMovingAverage(self.atr[i], period=30) for i in range(len(self.datas))]
        self.rsi = [bt.indicators.RSI(d.close, period=self.p.rsi_period) for d in self.datas]
        self.smal = [bt.indicators.SimpleMovingAverage(d.close, period=self.p.sma_long) for d in self.datas]
        self.smar = [bt.indicators.SimpleMovingAverage(d.close, period=self.p.sma_regime) for d in self.datas]
        self.bref = [d.lines.close for d in self.datas]

        # model book: index -> dict(side,size,entry,stop,peak,be,partial,entry_i,atr,rets)
        self.book = {}
        self.trades = []          # open records
        self.closes = []          # {j, reason, est_px}
        self.last_rotate = -10 ** 9
        self.bankrupt = False     # equity crossed <= 0 -> liquidated, no new trades

    # ── order callbacks ─────────────────────────────────────────────────────

    def notify_order(self, order):
        pass

    # ── helpers ──────────────────────────────────────────────────────────────

    def _returns(self, i):
        closes = self.bref[i].get(size=32)  # oldest -> newest
        rets = []
        for k in range(1, len(closes)):
            if closes[k - 1]:
                rets.append((closes[k] - closes[k - 1]) / closes[k - 1])
        return rets[-30:]

    def _num_open(self):
        return len(self.book)

    def _regime(self):
        """bull / bear / chop from BTC (datas[0]) on the PREVIOUS close."""
        sma200 = self.smal[0][-1]
        if math.isnan(sma200) or sma200 <= 0:
            return "unknown"
        prev_close = self.bref[0][-1]
        if prev_close > sma200:
            return "bull"
        sma50 = self.smar[0][-1]
        if math.isnan(sma50) or sma50 < sma200:
            return "bear"
        return "chop"

    # ── main loop ───────────────────────────────────────────────────────────

    def next(self):
        i = len(self.datas[0])
        prices = {j: self.bref[j][0] for j in range(len(self.datas))}

        # ── 0. Liquidation: mark-to-market equity crossed <= 0 ──
        mtm_eq = self.broker.getvalue() / SCALE
        if not self.bankrupt and mtm_eq <= 0:
            self.bankrupt = True
            print(f"  LIQUIDATION {self.datas[0].datetime.datetime(0).date()} "
                  f"mtm_eq=${mtm_eq:,.0f}", flush=True)
            # force-close by the broker's ACTUAL position size (a buy/sell that
            # exactly matches the open position has opened==0, so it fills even
            # when cash is deeply negative; a book-based size could overshoot
            # and be rejected by the broker's cash check)
            for j in list(self.book.keys()):
                pos = self.book[j]
                bsize = abs(self.getposition(self.datas[j]).size)
                if bsize <= 0:
                    continue
                (self.sell if pos["side"] == "long" else self.buy)(data=self.datas[j], size=bsize)
                self.closes.append({"j": j, "reason": "liquidation",
                                    "est_px": self.bref[j][0], "size": bsize})
            self.book = {}
            return
        if self.bankrupt:
            return

        # ── 1. Manage open positions (check current bar H/L, send closes) ──
        for j in list(self.book.keys()):
            pos = self.book[j]
            d = self.datas[j]
            row_h, row_l, row_c = d.high[0], d.low[0], d.close[0]
            trail = pos["atr"] * self.p.trail_atr_mult
            if trail <= 0:
                trail = pos["entry"] * 0.02

            hit, exit_raw, reason = False, None, "trail_stop"

            if pos["side"] == "long":
                if row_l <= pos["stop"]:
                    hit, exit_raw, reason = True, pos["stop"], "stop_loss"
                else:
                    if row_h > pos["peak"]:
                        pos["peak"] = row_h
                        ns = pos["peak"] - trail
                        if ns > pos["stop"]:
                            pos["stop"] = ns
                    if not pos["be"] and row_c >= pos["entry"] * (1 + self.p.breakeven_pct):
                        pos["stop"] = max(pos["stop"], pos["entry"] * 0.999)
                        pos["be"] = True
                    # staged partials: stage0 -> TP1, stage1 -> TP2 (if enabled)
                    if pos["partial_stage"] == 0 and row_h >= pos["entry"] * (1 + self.p.partial_tp_pct):
                        exit_raw, reason = pos["entry"] * (1 + self.p.partial_tp_pct), "partial_tp"
                        hit = True
                    elif (pos["partial_stage"] == 1 and self.p.partial_tp2_pct > 0
                          and row_h >= pos["entry"] * (1 + self.p.partial_tp2_pct)):
                        exit_raw, reason = pos["entry"] * (1 + self.p.partial_tp2_pct), "partial_tp2"
                        hit = True
            else:
                if row_h >= pos["stop"]:
                    hit, exit_raw, reason = True, pos["stop"], "stop_loss"
                else:
                    if row_l < pos["peak"]:
                        pos["peak"] = row_l
                        ns = pos["peak"] + trail
                        if ns < pos["stop"]:
                            pos["stop"] = ns
                    if not pos["be"] and row_c <= pos["entry"] * (1 - self.p.breakeven_pct):
                        pos["stop"] = min(pos["stop"], pos["entry"] * 1.001)
                        pos["be"] = True
                    if pos["partial_stage"] == 0 and row_l <= pos["entry"] * (1 - self.p.partial_tp_pct):
                        exit_raw, reason = pos["entry"] * (1 - self.p.partial_tp_pct), "partial_tp"
                        hit = True
                    elif (pos["partial_stage"] == 1 and self.p.partial_tp2_pct > 0
                          and row_l <= pos["entry"] * (1 - self.p.partial_tp2_pct)):
                        exit_raw, reason = pos["entry"] * (1 - self.p.partial_tp2_pct), "partial_tp2"
                        hit = True

            if hit and reason in ("partial_tp", "partial_tp2"):
                if reason == "partial_tp" and pos["partial_stage"] == 0:
                    ratio = self.p.partial_tp_ratio
                    close_size = pos["size"] * ratio
                    if close_size > 0:
                        self._close(j, close_size, reason, exit_raw)
                        pos["size"] -= close_size
                        pos["partial_stage"] = 1
                    hit = False
                elif reason == "partial_tp2" and pos["partial_stage"] == 1:
                    ratio = self.p.partial_tp2_ratio
                    close_size = pos["size"] * ratio
                    if close_size > 0:
                        self._close(j, close_size, reason, exit_raw)
                        pos["size"] -= close_size
                        pos["partial_stage"] = 2
                    hit = False

            # dynamic ROI exit
            if not hit:
                hold = i - pos["entry_i"]
                pnl_now = ((row_c / pos["entry"] - 1) * 100) if pos["side"] == "long" \
                    else ((pos["entry"] / row_c - 1) * 100)
                tp = roi_target(hold)
                if pnl_now > 0 and pnl_now >= tp * 100:
                    hit, exit_raw, reason = True, row_c, "roi"

            if hit:
                self._close(j, pos["size"], reason, exit_raw)
                del self.book[j]

        # ── 2. Rotation (cooldown when all slots full) ──
        if i - self.last_rotate < self.p.min_hold_days and self._num_open() >= self.p.top_k:
            return

        regime = self._regime()

        ranked = []
        for j in range(len(self.datas)):
            atr_prev = self.atr[j][-1]
            if math.isnan(atr_prev) or atr_prev <= 0:
                continue
            avg30 = self.atr30[j][-1]
            if not math.isnan(avg30) and avg30 > 0 and atr_prev > avg30 * self.p.vol_mult:
                continue

            ema_t = self.emaf[j][-1] > self.emas[j][-1]
            rsi_v = self.rsi[j][-1]
            roc_v = self.roc[j][-1] * 100.0     # RateOfChange is a fraction
            adx_v = self.adx[j][-1]

            if math.isnan(roc_v) or math.isnan(adx_v) or math.isnan(rsi_v):
                continue

            if rsi_v > self.p.rsi_long_max and ema_t:
                continue
            if rsi_v < self.p.rsi_short_min and not ema_t:
                continue
            if abs(roc_v) < self.p.min_roc:
                continue

            ema_s = self.emas[j][-1]
            trend_val = (self.emaf[j][-1] - ema_s) / ema_s * 100 if ema_s > 0 else 0.0
            score = roc_v * 0.5 + trend_val * 0.3 + (adx_v / 50) * 0.2
            ranked.append({"j": j, "score": score, "roc": roc_v, "ema_t": ema_t,
                           "adx": adx_v, "atr": atr_prev, "rets": self._returns(j)})

        ranked.sort(key=lambda x: x["score"], reverse=True)

        targets = []
        for row in ranked:
            if len(targets) >= self.p.top_k:
                break
            if regime in ("bull", "unknown"):
                if row["roc"] > self.p.min_roc and row["ema_t"] and row["adx"] >= self.p.adx_min:
                    side = "long"
                else:
                    continue
            elif regime == "bear":
                if self.p.allow_short and row["roc"] < -self.p.min_roc and not row["ema_t"] \
                        and row["adx"] >= self.p.adx_min:
                    side = "short"
                else:
                    continue
            else:
                continue
            corr_ok = True
            check_against = [self.book[p]["rets"] for p in self.book] + [t["rets"] for t in targets]
            for held in check_against:
                if abs(_corr(row["rets"], held)) > self.p.corr_threshold:
                    corr_ok = False
                    break
            if not corr_ok:
                continue
            targets.append({"j": row["j"], "side": side, "atr": row["atr"], "rets": row["rets"]})

        # ── 3. Close rotated-out positions when all slots are full ──
        if self._num_open() >= self.p.top_k:
            target_set = {(t["j"], t["side"]) for t in targets}
            for j in list(self.book.keys()):
                pos = self.book[j]
                if (j, pos["side"]) in target_set:
                    continue
                self._close(j, pos["size"], "rotation_exit", self.datas[j].open[0])
                del self.book[j]

        # ── 4. Open new at next open ──
        for t in targets:
            j = t["j"]
            if j in self.book:
                continue
            price = self.bref[j][0]
            atr_val = t["atr"]
            stop_dist = atr_val * self.p.atr_stop_mult
            if t["side"] == "long":
                stop = price - stop_dist
            else:
                stop = price + stop_dist
            if stop_dist <= 0 or price <= 0:
                continue

            # risk-based sizing (leverage-capped), identical to live bot
            equity = self.broker.getvalue() / SCALE
            atr_pct = atr_val / price
            lev = max(1.0, min(self.p.max_leverage, 1.0 / (atr_pct * 2)))
            risk_usd = equity * self.p.risk_per_trade
            notional = risk_usd / (stop_dist / price)
            margin = notional / lev if lev > 0 else notional
            max_margin = equity * self.p.allocation_pct
            if margin > max_margin:
                margin = max_margin
                notional = margin * lev
            size = notional / price * SCALE   # scale sizes so cash & pnl are in the same units
            if size <= 0:
                continue

            self.book[j] = {
                "side": t["side"], "size": size, "entry": price, "stop": stop,
                "peak": price, "be": False, "partial_stage": 0,
                "entry_i": i, "atr": atr_val, "rets": t["rets"],
            }
            if t["side"] == "long":
                self.buy(data=self.datas[j], size=size)
            else:
                self.sell(data=self.datas[j], size=size)
            self.trades.append({
                "j": j, "name": self.datas[j]._name, "side": t["side"],
                "entry": price, "entry_i": i, "reason": "open", "size": size, "pnl": None,
            })
            self.last_rotate = i

    # ── execution helper ────────────────────────────────────────────────────

    def _close(self, j, size, reason, est_px):
        """Send a market order to close `size`. Actual fill happens at the next
        open and is handled by the bt broker; est_px only used for the report."""
        if size <= 0:
            return
        pos = self.book.get(j)
        if pos is None:
            return
        # never close more than the broker actually holds (keeps book in sync
        # even if an earlier close was rejected by a negative-cash check)
        bsize = abs(self.getposition(self.datas[j]).size)
        if bsize <= 0:
            return
        size = min(size, bsize)
        side = pos["side"]
        (self.sell if side == "long" else self.buy)(data=self.datas[j], size=size)
        self.closes.append({"j": j, "reason": reason, "est_px": est_px})


class EquityCurve(bt.Analyzer):
    def start(self):
        self.vals = []

    def next(self):
        self.vals.append((self.strategy.datetime.datetime(0), self.strategy.broker.getvalue()))

    def get_analysis(self):
        return {"curve": self.vals}


# ── runner ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Momentum Rotation v4 — Backtrader")
    ap.add_argument("--pairs", default="BTC,ETH,BNB,SOL", help="comma-separated coins")
    ap.add_argument("--days", type=int, default=400, help="backtest window (daily bars)")
    ap.add_argument("--capital", type=float, default=10000.0)
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--set", nargs="*", default=[],
                    help="param overrides, e.g. --set top_k=2 risk_per_trade=0.20")
    p = ap.parse_args()

    overrides = {}
    for s in p.set:
        k, v = s.split("=", 1)
        if v.lower() in ("true", "false"):
            overrides[k] = v.lower() == "true"
        else:
            try:
                overrides[k] = float(v)
            except ValueError:
                overrides[k] = v

    coins = [c.strip() for c in p.pairs.split(",") if c.strip()]
    insts = [SWAP_MAP.get(c, c + "-USDT-SWAP") for c in coins]

    print(f"Загрузка {len(insts)} инструментов с OKX ({p.days} дней)...")
    raw = bt_okx.run_sync(bt_okx.load_universe(insts, p.tf, p.days))
    if not raw:
        print("Нет данных")
        return
    raw = bt_okx.align(raw)
    print(f"Общий диапазон: {next(iter(raw.values())).index[0].date()} -> "
          f"{next(iter(raw.values())).index[-1].date()}")

    cerebro = bt.Cerebro(stdstats=False)
    cerebro.addstrategy(MomentumRotationV4, verbose=p.verbose, **overrides)
    for inst, df in raw.items():
        cerebro.adddata(bt_okx.as_bt_feed(df, name=inst), name=inst)
    # Inflated cash -> scaled sizes -> real leverage works; report real $ later.
    cerebro.broker.setcash(p.capital * SCALE)
    cerebro.broker.set_checksubmit(False)  # liquidation closes must fill even with negative cash
    cerebro.broker.setcommission(commission=COMMISSION, commtype=bt.CommInfoBase.COMM_PERC,
                                 percabs=True, stocklike=True)
    cerebro.broker.set_slippage_perc(perc=SLIPPAGE)
    cerebro.addanalyzer(EquityCurve, _name="eq")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

    print("Запуск backtest...")
    results = cerebro.run()
    strat = results[0]
    start = p.capital
    end = strat.broker.getvalue() / SCALE
    # liquidation floor: the account is worth 0 (not negative) once wiped
    if strat.bankrupt:
        end = 0.0
    curve = [(ts, max(0.0, v / SCALE)) for ts, v in strat.analyzers.eq.get_analysis()["curve"]]
    years = (curve[-1][0] - curve[0][0]).days / 365.25 if len(curve) > 1 else 0.0
    total_ret = (end / start - 1) * 100 if end > 0 else -100.0
    cagr = ((end / start) ** (1 / years) - 1) * 100 if years > 0 and end > 0 else 0.0
    ta = strat.analyzers.trades.get_analysis()

    # % metrics computed on the REAL equity curve (scaled back to capital base),
    # because the raw broker base is SCALE * capital.
    eq = np.array([v for _, v in curve], dtype=float)
    mask = eq[:-1] > 0  # after liquidation the value stays 0 -> no division by 0
    rets = np.zeros_like(eq[:-1], dtype=float)
    np.divide(np.diff(eq), eq[:-1], out=rets, where=mask)
    sharpe = (rets.mean() / rets.std() * np.sqrt(365)) if len(rets) > 1 and rets.std() > 0 else 0.0
    peak = np.maximum.accumulate(eq)
    dd_series = (eq - peak) / peak
    max_dd = dd_series.min() * 100

    closed_total = ta.total.closed if ta.total else 0
    won = ta.won.total if ta.won else 0
    lost = ta.lost.total if ta.lost else 0
    print()
    print("=" * 64)
    print("  MOMENTUM ROTATION v4 — BACKTRADER (реальные OKX свечи)")
    print("=" * 64)
    print(f"  Данные:        {curve[0][0].date()} -> {curve[-1][0].date()} "
          f"({years:.2f} года)")
    if strat.bankrupt:
        print(f"  СТАТУС:        ЛИКВИДАЦИЯ (equity <= 0, trading остановлен)")
    print(f"  Капитал:       ${start:,.0f} -> ${end:,.2f}")
    print(f"  Total return:  {total_ret:+.2f}%")
    print(f"  CAGR:          {cagr:+.2f}%")
    print(f"  Sharpe (ann):  {sharpe:.2f}")
    print(f"  Max drawdown:  {max_dd:.2f}%")
    print(f"  Сделок закрыто: {closed_total}  (win {won} / loss {lost})")
    if ta.pnl:
        print(f"  Net PnL:       ${ta.pnl.net.total/SCALE:,.0f}  "
              f"(avg win ${ta.won.pnl.average/SCALE:.1f} / "
              f"avg loss ${ta.lost.pnl.average/SCALE:.1f})")
    print(f"  Записей входа: {sum(1 for t in strat.trades if t['reason']=='open')}")
    by_reason = {}
    for c in strat.closes:
        by_reason[c["reason"]] = by_reason.get(c["reason"], 0) + 1
    print(f"  Причины выходов:")
    for r, n in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"    {r:16s} {n}")

    # Yearly returns on the REAL equity curve (first->last value of each year)
    by_year = {}
    for ts, v in curve:
        by_year.setdefault(ts.year, []).append(v)
    print("  Годовая доходность:")
    for y in sorted(by_year):
        vals = by_year[y]
        tag = ""
        if y == curve[0][0].year:
            tag = "  (с " + curve[0][0].strftime("%d.%m") + ")"
        if y == curve[-1][0].year:
            tag = "  (по " + curve[-1][0].strftime("%d.%m") + ")"
        if vals[0] > 0:
            yr_ret = (vals[-1] / vals[0] - 1) * 100
            print(f"    {y}: {yr_ret:+7.1f}%   ${vals[0]:>12,.0f} -> ${vals[-1]:>12,.0f}{tag}")
        else:
            print(f"    {y}:  банкротство (-> $0){tag}  ${vals[0]:>12,.0f} -> ${vals[-1]:>12,.0f}")
    print("=" * 64)
    print("  Вход/выход исполняются по открытию следующего бара; стоп")
    print("  срабатывает на текущем баре, закрытие — на следующем open.")
    print("  Комиссия 0.1%/сторона + проскальзывание 0.05%; цена фьючерсов")
    print("  OKX-SWAP, сигнал по закрытию предыдущего бара (без подглядывания).")


if __name__ == "__main__":
    main()
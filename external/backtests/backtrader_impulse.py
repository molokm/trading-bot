#!/usr/bin/env python3
"""Impulse 1D v1 — Backtrader (bt) engine.

Independent re-implementation of the live `impulse_strategy.py` (v1) on top of
Backtrader's cerebro framework. Fetches real OKX daily candles, runs the impulse
logic as a bt.Strategy and reports metrics via bt built-in analyzers
(TradeAnalyzer) + broker value curve.

Reference benchmark: `scripts/impulse_bot_backtest.py` (BASE config, 1d window),
live docstring full-window stats: +530.6%, CAGR 72.1%, MaxDD 31.0%, Sharpe 1.26.

Strategy (mirrors the pandas reference):
  * Entry: 1-day |ROC| >= 4% + volume surge (>= 1.5x avg) + EMA20>EMA50 trend;
    short on the symmetric downside impulse. Rank by |ROC|, top_k=4 slots.
  * Pyramiding: up to 2 adds within a 5-day window on a new peak (>= +0.5 ATR)
    with a volume surge; each add = 60% of the current position.
  * Cascade take-profit: 30% at +2 ATR, then 30% at +6 ATR (checked at the open).
  * Stops: initial = entry -/+ 5 x entry ATR; breakeven after +0.5%;
    trailing = peak -/+ 8 x entry ATR; time exit after 30 days.
  * Risk-per-trade 10% of equity, dynamic leverage (<= 3x), margin cap 50%.

Modeling notes (same conventions as backtrader_momentum_rotation.py):
  * Signals are computed on the PREVIOUS completed bar close (bt line[-1]) and
    orders are market orders that fill at the NEXT bar open -> a 1-bar-later
    (conservative) shift vs the pandas engine which fills at today's open.
  * Stops/trails are decided on the current bar H/L and the close order fills at
    the next open (stops act as triggers, not guaranteed intrabar fills).
  * One TP/TP-add adjustment per bar (first of TP1/TP2 wins), then management.
  * Commission 0.10%/side + slippage 0.05% applied by the bt broker. Funding is
    omitted (daily bars, no funding cache).

Usage:
  python backtrader_impulse.py                      # BTC/ETH/BNB/SOL, 400d, $10k
  python backtrader_impulse.py --pairs BTC,ETH,BNB,SOL,XRP,DOGE,ADA,TRX,AVAX,LTC --days 1100
"""

import argparse
import math

import backtrader as bt
import numpy as np
import bt_okx

# Backtrader broker requires cash >= notional even when the strategy uses
# leverage; sizes are scaled by SCALE so leveraged exposure never trips the
# broker's margin check. All reported $ figures are divided back by SCALE.
SCALE = 100

# ── Defaults mirroring ImpulseConfig BASE (daily), tuned 2026-08 ────────────
# Tuned config (safe/live variant): top_k=3, max_adds=0, cooldown=3,
# entry_roc=3.0, trail=12 ATR, tp2=10 ATR, risk=0.10.
# Validated on OKX native 1D, 10 coins, 2023-05..2026-08:
#   BT (live-faithful):  CAGR ~63%, Sharpe 1.58, MaxDD ~-36%.
TOP_K = 3
IMPULSE_BARS = 1
ENTRY_ROC = 3.0
RSI_CONF_MIN = 0.0
RSI_CONF_MAX = 100.0
EMA_FAST = 20
EMA_SLOW = 50
VOL_MULT = 1.5
VOL_PERIOD = 24
MAX_ADDS = 0
ADD_SIZE_RATIO = 0.6
ADD_WINDOW_BARS = 5
ADD_ATR_MULT = 0.5
MAX_LEVERAGE = 3.0
RISK_PER_TRADE = 0.10
SL_ATR_MULT = 5.0
TRAIL_ATR_MULT = 12.0
BE_PCT = 0.005
COOLDOWN_BARS = 3
TP1_ATR = 2.0
TP1_FRAC = 0.3
TP2_ATR = 10.0
TP2_FRAC = 0.3
MAX_HOLD_BARS = 30
ALLOW_SHORT = True
MAX_MARGIN_PCT = 0.5
# regime filter (BTC): 0=off, 1=direction (bull:long/bear:short/chop:both),
# 2=direction + stand aside in chop, 3=longs always, shorts only in bear
REGIME_MODE = 0
REGIME_SMA_LONG = 200
REGIME_SMA_SHORT = 50
COMMISSION = 0.001
SLIPPAGE = 0.0005

SWAP_MAP = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP", "BNB": "BNB-USDT-SWAP",
            "SOL": "SOL-USDT-SWAP", "XRP": "XRP-USDT-SWAP", "DOGE": "DOGE-USDT-SWAP",
            "ADA": "ADA-USDT-SWAP", "TRX": "TRX-USDT-SWAP", "AVAX": "AVAX-USDT-SWAP",
            "LTC": "LTC-USDT-SWAP"}


class Impulse1D(bt.Strategy):
    """Fast momentum entry + pyramiding + cascade exit (daily bars)."""

    params = (
        ("top_k", TOP_K), ("impulse_bars", IMPULSE_BARS), ("entry_roc", ENTRY_ROC),
        ("rsi_conf_min", RSI_CONF_MIN), ("rsi_conf_max", RSI_CONF_MAX),
        ("ema_fast", EMA_FAST), ("ema_slow", EMA_SLOW),
        ("vol_mult", VOL_MULT), ("vol_period", VOL_PERIOD),
        ("max_adds", MAX_ADDS), ("add_size_ratio", ADD_SIZE_RATIO),
        ("add_window_bars", ADD_WINDOW_BARS), ("add_atr_mult", ADD_ATR_MULT),
        ("max_leverage", MAX_LEVERAGE), ("risk_per_trade", RISK_PER_TRADE),
        ("sl_atr_mult", SL_ATR_MULT), ("trail_atr_mult", TRAIL_ATR_MULT),
        ("be_pct", BE_PCT), ("cooldown_bars", COOLDOWN_BARS),
        ("tp1_atr", TP1_ATR), ("tp1_frac", TP1_FRAC),
        ("tp2_atr", TP2_ATR), ("tp2_frac", TP2_FRAC),
        ("max_hold_bars", MAX_HOLD_BARS), ("allow_short", ALLOW_SHORT),
        ("max_margin_pct", MAX_MARGIN_PCT), ("verbose", False),
        ("regime_mode", REGIME_MODE), ("regime_sma_long", REGIME_SMA_LONG),
        ("regime_sma_short", REGIME_SMA_SHORT),
    )

    def __init__(self):
        self.emaf = [bt.indicators.EMA(d.close, period=self.p.ema_fast) for d in self.datas]
        self.emas = [bt.indicators.EMA(d.close, period=self.p.ema_slow) for d in self.datas]
        self.atr = [bt.indicators.ATR(d, period=14) for d in self.datas]
        self.rsi = [bt.indicators.RSI(d.close, period=14) for d in self.datas]
        self.roc = [bt.indicators.RateOfChange(d.close, period=self.p.impulse_bars) for d in self.datas]
        self.vol = [d.volume for d in self.datas]
        self.svol = [bt.indicators.SimpleMovingAverage(d.volume, period=self.p.vol_period) for d in self.datas]
        self.bref = [d.lines.close for d in self.datas]

        # book: index -> dict(side, size, entry, stop, peak, atr, breakeven,
        #                      tp1_done, tp2_done, adds, last_add_peak, entry_i)
        self.book = {}
        self.trades = []          # open / add records
        self.closes = []          # {j, reason, est_px, size}
        self.last_exit = {}       # j -> bar index of last exit (cooldown)

    # ── order callbacks ─────────────────────────────────────────────────────

    def notify_order(self, order):
        pass

    # ── helpers ──────────────────────────────────────────────────────────────

    def _lev(self, atr_pct):
        if atr_pct <= 0:
            return 1.0
        return max(1.0, min(self.p.max_leverage, 1.0 / (atr_pct * 2)))

    def _regime(self):
        """BTC regime on the PREVIOUS close: bull / bear / chop / unknown.

        Computed manually from the close buffer so it does NOT raise the
        strategy's minperiod (adding SMA indicators would delay next() by
        regime_sma_long bars and change the trading window).
        """
        closes = list(self.datas[0].close.get(size=self.p.regime_sma_long + 1))
        if len(closes) < self.p.regime_sma_long + 1:
            return "unknown"
        c = closes[-2]                                  # previous close
        window = closes[:-1]                            # up to previous bar
        if any(x is None or math.isnan(x) for x in window):
            return "unknown"
        sma200 = sum(window[-self.p.regime_sma_long:]) / self.p.regime_sma_long
        if c > sma200:
            return "bull"
        k = min(self.p.regime_sma_short, len(window))
        sma50 = sum(window[-k:]) / k
        if sma50 < sma200:
            return "bear"
        return "chop"

    def _regime_ok(self, side):
        """Apply regime_mode to a candidate side. None => block the entry."""
        mode = self.p.regime_mode
        if mode == 0:
            return True
        reg = self._regime()
        if reg == "unknown":
            return True
        if mode == 1:  # direction
            if reg == "bull":
                return side == "long"
            if reg == "bear":
                return side == "short"
            return True  # chop: both
        if mode == 2:  # direction + stand aside in chop
            if reg == "bull":
                return side == "long"
            if reg == "bear":
                return side == "short"
            return False
        if mode == 3:  # longs always; shorts only in strong downtrend (bear)
            if reg == "bull":
                return side == "long"
            if reg == "bear":
                return True
            return side == "long"  # chop: no shorts
        return True

    def _open_size(self, price, stop_dist, atr_val):
        """Risk-based sizing in SCALED base units (identical math to live bot).

        NOTE: the live bot sizes leverage from the RAW atr/price ratio
        (_calc_dynamic_leverage), NOT from the stop distance — using
        stop_dist/price here would shrink leverage by sl_atr_mult and
        under-report PnL."""
        equity = self.broker.getvalue() / SCALE
        stop_pct = stop_dist / price if price > 0 else 0.03
        lev = self._lev(atr_val / price if price > 0 and atr_val > 0 else 0.0)
        notional = (equity * self.p.risk_per_trade) / stop_pct
        margin = notional / lev if lev > 0 else notional
        max_margin = equity * self.p.max_margin_pct
        if margin > max_margin:
            margin = max_margin
            notional = margin * lev
        return notional / price * SCALE

    def _add_size(self, pos, price):
        """Pyramid slice: 60% of current position, capped by margin."""
        equity = self.broker.getvalue() / SCALE
        base = (pos["size"] / SCALE) * self.p.add_size_ratio
        lev = self._lev(pos["atr"] / price if price > 0 else 0.0)
        margin = (base * price) / lev if lev > 0 else base * price
        max_margin = equity * self.p.max_margin_pct
        if margin > max_margin:
            base = max_margin * lev / price
        return base * SCALE

    # ── main loop ───────────────────────────────────────────────────────────

    def next(self):
        i = len(self.datas[0])

        # warmup gate — identical to the pandas reference so both engines start
        # trading on the same bar (live bot's own gate is ema_slow+10, but the
        # reference uses max(ema_slow+vol_period+10, impulse_bars+30))
        warmup = max(self.p.ema_slow + self.p.vol_period + 10,
                     self.p.impulse_bars + 30)
        if i <= warmup:
            return

        # ── 1. Adjust open positions: cascade TP, then pyramiding ──
        for j in list(self.book.keys()):
            pos = self.book[j]
            d = self.datas[j]
            atr = pos["atr"]
            if atr <= 0:
                continue
            o = d.open[0]
            if pos["side"] == "long":
                dist_atr = (o - pos["entry"]) / atr
            else:
                dist_atr = (pos["entry"] - o) / atr

            acted = False
            if not pos["tp1_done"] and dist_atr >= self.p.tp1_atr:
                self._reduce(j, pos["size"] * self.p.tp1_frac, "tp1")
                pos["tp1_done"] = True
                acted = True
            elif not pos["tp2_done"] and dist_atr >= self.p.tp2_atr:
                self._reduce(j, pos["size"] * self.p.tp2_frac, "tp2")
                pos["tp2_done"] = True
                acted = True
            if acted or pos["size"] <= 0:
                if pos["size"] <= 0:
                    del self.book[j]
                continue

            # pyramiding add
            if pos["adds"] < self.p.max_adds and i - pos["entry_i"] <= self.p.add_window_bars:
                if pos["side"] == "long":
                    new_peak = o >= pos["last_add_peak"] + atr * self.p.add_atr_mult
                else:
                    new_peak = o <= pos["last_add_peak"] - atr * self.p.add_atr_mult
                avg_vol = self.svol[j][-1]
                vol_surge = avg_vol > 0 and self.vol[j][-1] >= avg_vol * self.p.vol_mult
                if new_peak and vol_surge:
                    add_sz = self._add_size(pos, o)
                    if add_sz > 0:
                        if pos["side"] == "long":
                            self.buy(data=d, size=add_sz)
                        else:
                            self.sell(data=d, size=add_sz)
                        old_cost = pos["size"] * pos["entry"]
                        pos["size"] += add_sz
                        pos["entry"] = (old_cost + add_sz * o) / pos["size"]
                        pos["adds"] += 1
                        pos["last_add_peak"] = o
                        self.trades.append({
                            "j": j, "name": d._name, "side": pos["side"],
                            "entry": o, "entry_i": i, "reason": "add", "size": add_sz,
                        })
                        if self.p.verbose:
                            print(f"  ADD  {d._name} {pos['side']} @ {o:.2f} "
                                  f"add={add_sz/SCALE:.4f} tot={pos['size']/SCALE:.4f} "
                                  f"entry={pos['entry']:.2f} adds={pos['adds']}")

        # ── 2. Manage open positions: stop / trail / breakeven / time exit ──
        for j in list(self.book.keys()):
            pos = self.book[j]
            if pos["size"] <= 0:
                del self.book[j]
                continue
            d = self.datas[j]
            atr = pos["atr"]
            o, h, l = d.open[0], d.high[0], d.low[0]

            hit, reason = False, "stop_loss"
            if pos["side"] == "long":
                if l <= pos["stop"]:
                    hit = True
                else:
                    if h > pos["peak"]:
                        pos["peak"] = h
                        ns = pos["peak"] - atr * self.p.trail_atr_mult
                        if ns > pos["stop"]:
                            pos["stop"] = ns
                    if not pos["breakeven"] and self.bref[j][-1] >= pos["entry"] * (1 + self.p.be_pct):
                        pos["breakeven"] = True
                    if pos["breakeven"]:
                        pos["stop"] = max(pos["stop"], pos["entry"] * 0.999)
            else:
                if h >= pos["stop"]:
                    hit = True
                else:
                    if l < pos["peak"]:
                        pos["peak"] = l
                        ns = pos["peak"] + atr * self.p.trail_atr_mult
                        if ns < pos["stop"]:
                            pos["stop"] = ns
                    if not pos["breakeven"] and self.bref[j][-1] <= pos["entry"] * (1 - self.p.be_pct):
                        pos["breakeven"] = True
                    if pos["breakeven"]:
                        pos["stop"] = min(pos["stop"], pos["entry"] * 1.001)

            if not hit and i - pos["entry_i"] >= self.p.max_hold_bars:
                hit, reason = True, "time_exit"

            if hit and pos["size"] > 0:
                est = pos["stop"] if reason == "stop_loss" else o
                self._reduce(j, pos["size"], reason, est)
                self.last_exit[j] = i
                del self.book[j]

        # ── 3. New entries from the previous bar close signal ──
        candidates = []
        for j in range(len(self.datas)):
            if j in self.book:
                continue
            if i - self.last_exit.get(j, -10 ** 9) < self.p.cooldown_bars:
                continue
            roc_v = self.roc[j][-1] * 100.0
            rsi_v = self.rsi[j][-1]
            atr_v = self.atr[j][-1]
            if math.isnan(roc_v) or math.isnan(rsi_v) or math.isnan(atr_v) or atr_v <= 0:
                continue
            avg_vol = self.svol[j][-1]
            if math.isnan(avg_vol) or avg_vol <= 0:
                continue
            if self.vol[j][-1] < avg_vol * self.p.vol_mult:
                continue
            ema_t = self.emaf[j][-1] > self.emas[j][-1]
            side = None
            if roc_v >= self.p.entry_roc and ema_t \
                    and self.p.rsi_conf_min <= rsi_v <= self.p.rsi_conf_max:
                side = "long"
            elif self.p.allow_short and roc_v <= -self.p.entry_roc and not ema_t \
                    and (100 - self.p.rsi_conf_max) <= rsi_v <= (100 - self.p.rsi_conf_min):
                side = "short"
            if not side:
                continue
            if not self._regime_ok(side):
                continue
            candidates.append({"j": j, "side": side, "atr": atr_v, "roc": roc_v})

        candidates.sort(key=lambda x: -abs(x["roc"]))
        for t in candidates:
            if len(self.book) >= self.p.top_k:
                break
            j = t["j"]
            if j in self.book:
                continue
            d = self.datas[j]
            price = d.open[0]
            atr_val = t["atr"]
            stop_dist = atr_val * self.p.sl_atr_mult
            if stop_dist <= 0 or price <= 0:
                continue
            if t["side"] == "long":
                stop = price - stop_dist
            else:
                stop = price + stop_dist

            size = self._open_size(price, stop_dist, atr_val)
            if size <= 0:
                continue

            self.book[j] = {
                "side": t["side"], "size": size, "entry": price, "stop": stop,
                "peak": price, "atr": atr_val, "breakeven": False,
                "tp1_done": False, "tp2_done": False, "adds": 0,
                "last_add_peak": price, "entry_i": i,
            }
            if t["side"] == "long":
                self.buy(data=d, size=size)
            else:
                self.sell(data=d, size=size)
            self.trades.append({
                "j": j, "name": d._name, "side": t["side"], "entry": price,
                "entry_i": i, "reason": "open", "size": size,
            })
            if self.p.verbose:
                print(f"  OPEN {d._name:12} {t['side']:5} @ {price:.2f} stop={stop:.2f} "
                      f"sz={size/SCALE:.4f} atr={atr_val:.2f} roc={t['roc']:.1f}")

    # ── execution helper ────────────────────────────────────────────────────

    def _reduce(self, j, size, reason, est_px=None):
        """Send a market order to reduce the position by `size`. Actual fill
        happens at the next open; est_px only used for the report."""
        if size <= 0:
            return
        pos = self.book.get(j)
        if pos is None:
            return
        # never reduce more than the broker actually holds (keeps book in sync
        # even if an earlier close was rejected by a negative-cash check)
        bsize = abs(self.getposition(self.datas[j]).size)
        if bsize <= 0:
            return
        size = min(size, bsize)
        side = pos["side"]
        d = self.datas[j]
        (self.sell if side == "long" else self.buy)(data=d, size=size)
        self.closes.append({"j": j, "reason": reason, "est_px": est_px, "size": size})
        pos["size"] -= size
        if self.p.verbose:
            if est_px is not None:
                print(f"  CLOSE {d._name:12} {reason:10} size={size/SCALE:.4f} "
                      f"rem={pos['size']/SCALE:.4f} est@{est_px:.2f}")
            else:
                print(f"  CLOSE {d._name:12} {reason:10} size={size/SCALE:.4f} "
                      f"rem={pos['size']/SCALE:.4f}")


class EquityCurve(bt.Analyzer):
    def start(self):
        self.vals = []

    def next(self):
        self.vals.append((self.strategy.datetime.datetime(0), self.strategy.broker.getvalue()))

    def get_analysis(self):
        return {"curve": self.vals}


# ── runner ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Impulse 1D v1 — Backtrader")
    ap.add_argument("--pairs", default="BTC,ETH,BNB,SOL", help="comma-separated coins")
    ap.add_argument("--days", type=int, default=400, help="backtest window (daily bars)")
    ap.add_argument("--capital", type=float, default=10000.0)
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--set", nargs="*", default=[],
                    help="param overrides, e.g. --set top_k=3 max_adds=0 entry_roc=3.0")
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
    cerebro.addstrategy(Impulse1D, verbose=p.verbose, **overrides)
    for inst, df in raw.items():
        cerebro.adddata(bt_okx.as_bt_feed(df, name=inst), name=inst)
    cerebro.broker.setcash(p.capital * SCALE)
    cerebro.broker.set_checksubmit(False)  # closes must fill even with negative cash
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
    curve = [(ts, v / SCALE) for ts, v in strat.analyzers.eq.get_analysis()["curve"]]
    years = (curve[-1][0] - curve[0][0]).days / 365.25 if len(curve) > 1 else 0.0
    total_ret = (end / start - 1) * 100
    cagr = ((end / start) ** (1 / years) - 1) * 100 if years > 0 and end > 0 else 0.0
    ta = strat.analyzers.trades.get_analysis()

    eq = np.array([v for _, v in curve], dtype=float)
    rets = np.diff(eq) / eq[:-1]
    sharpe = (rets.mean() / rets.std() * np.sqrt(365)) if len(rets) > 1 and rets.std() > 0 else 0.0
    peak = np.maximum.accumulate(eq)
    dd_series = (eq - peak) / peak
    max_dd = dd_series.min() * 100

    closed_total = ta.total.closed if ta.total else 0
    won = ta.won.total if ta.won else 0
    lost = ta.lost.total if ta.lost else 0

    open_end = [(d._name, round(strat.broker.getposition(d).size, 4))
                for d in strat.datas if strat.broker.getposition(d).size != 0]

    print()
    print("=" * 64)
    print("  IMPULSE 1D v1 — BACKTRADER (реальные OKX свечи)")
    print("=" * 64)
    print(f"  Данные:        {curve[0][0].date()} -> {curve[-1][0].date()} "
          f"({years:.2f} года)")
    print(f"  Капитал:       ${start:,.0f} -> ${end:,.2f}")
    print(f"  Total return:  {total_ret:+.2f}%")
    print(f"  CAGR:          {cagr:+.2f}%")
    print(f"  Sharpe (ann):  {sharpe:.2f}")
    print(f"  Max drawdown:  {max_dd:.2f}%")
    print(f"  Сделок закрыто: {closed_total}  (win {won} / loss {lost})")
    if open_end:
        print(f"  Открыто на конец: " + ", ".join(f"{n} {s:+.4f}" for n, s in open_end)
              + " (незакрытый PnL уже учтён в финальном капитале)")
    if ta.pnl:
        print(f"  Net PnL (реализ.): ${ta.pnl.net.total/SCALE:,.0f}  "
              f"(avg win ${ta.won.pnl.average/SCALE:.1f} / "
              f"avg loss ${ta.lost.pnl.average/SCALE:.1f})")
    print(f"  Записей входа: {sum(1 for t in strat.trades if t['reason']=='open')}  "
          f"(adds {sum(1 for t in strat.trades if t['reason']=='add')})")
    by_reason = {}
    for c in strat.closes:
        by_reason[c["reason"]] = by_reason.get(c["reason"], 0) + 1
    print(f"  Причины выходов:")
    for r, n in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"    {r:16s} {n}")
    print("=" * 64)
    print("  Сигнал по закрытию предыдущего бара; вход/выход исполняются")
    print("  по открытию следующего бара; стоп срабатывает на текущем баре,")
    print("  закрытие — на следующем open. Комиссия 0.1%/сторона + слпипп 0.05%.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""MACD+Donchian Validation — Backtrader (bt) engine.

Independent re-implementation of the live `macd_donchian_strategy.py`
(validation bot) on top of Backtrader's cerebro framework. Fetches real OKX
daily candles, runs the breakout logic as a bt.Strategy and reports metrics via
bt built-in analyzers (TradeAnalyzer) + broker value curve.

Modeling notes (same conventions as backtrader_momentum_rotation.py):
  * Signals are computed on the PREVIOUS completed bar close (bt line[-1]) and
    orders are market orders that fill at the NEXT bar open -> 1-bar-later
    (conservative) shift vs the pandas engine which fills at today's open.
  * Stops/trails are decided on the current bar H/L and the close order fills at
    the next open (stops act as triggers, not guaranteed intrabar fills).
  * Commission 0.10%/side + slippage 0.05% applied by the bt broker.
  * Sizing math is identical to the live bot (risk-based, leverage-capped,
    margin capped at `allocation_pct` of equity).

Usage:
  python backtrader_macd_donchian.py                          # 10 coins, 1100d, $10k
  python backtrader_macd_donchian.py --pairs BTC,ETH,BNB,XRP,SOL,DOGE,ADA,TRX,AVAX,LTC --days 1100
"""

import argparse
import math

import backtrader as bt
import numpy as np
import bt_okx

# Backtrader broker requires cash >= notional even when the strategy uses
# leverage. We inflate cash by SCALE and keep sizes in real units, so leveraged
# exposure never trips the broker's margin check. All reported $ figures are
# divided back by SCALE.
SCALE = 100

# ── Defaults mirroring ValidationStrategy (make_validation_config), tuned 2026-08 ──
TOP_K = 2
DONCHIAN_N = 30
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ATR_PERIOD = 14
MAX_LEVERAGE = 2.0
RISK_PER_TRADE = 0.14
ALLOCATION_PCT = 0.5
CHANDELIER_ATR = 4.0        # trail = peak - 4*ATR
TP_PCT = 0.08               # partial TP: close 20% at +8% (v2)
TP_RATIO = 0.4
TP2_PCT = 0.08              # second TP for remainder (full exit)
BE_PCT = 0.015              # breakeven for ALL positions at +1.5%
MAX_HOLD_DAYS = 3           # time exit
ALLOW_SHORT = False
COMMISSION = 0.001
SLIPPAGE = 0.0005

SWAP_MAP = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP", "BNB": "BNB-USDT-SWAP",
            "SOL": "SOL-USDT-SWAP", "XRP": "XRP-USDT-SWAP", "DOGE": "DOGE-USDT-SWAP",
            "ADA": "ADA-USDT-SWAP", "TRX": "TRX-USDT-SWAP", "AVAX": "AVAX-USDT-SWAP",
            "LTC": "LTC-USDT-SWAP"}


class MacdDonchian(bt.Strategy):
    """Donchian breakout + MACD confirmation + chandelier/breakeven exits."""

    params = (
        ("top_k", TOP_K), ("donchian_n", DONCHIAN_N),
        ("macd_fast", MACD_FAST), ("macd_slow", MACD_SLOW), ("macd_signal", MACD_SIGNAL),
        ("atr_period", ATR_PERIOD), ("max_leverage", MAX_LEVERAGE),
        ("risk_per_trade", RISK_PER_TRADE), ("allocation_pct", ALLOCATION_PCT),
        ("chandelier_atr", CHANDELIER_ATR), ("tp_pct", TP_PCT), ("tp_ratio", TP_RATIO),
        ("tp2_pct", TP2_PCT), ("be_pct", BE_PCT), ("max_hold_days", MAX_HOLD_DAYS),
        ("allow_short", ALLOW_SHORT), ("verbose", False),
    )

    def __init__(self):
        self.atr = [bt.indicators.ATR(d, period=self.p.atr_period) for d in self.datas]
        self.donchian = [bt.indicators.Highest(d.high, period=self.p.donchian_n) for d in self.datas]
        self.macd = [bt.indicators.MACD(d.close, period_me1=self.p.macd_fast,
                                        period_me2=self.p.macd_slow,
                                        period_signal=self.p.macd_signal) for d in self.datas]
        self.bref = [d.lines.close for d in self.datas]

        self.book = {}          # j -> dict(side, size, entry, stop, peak, atr, be_active,
                                #              partial_taken, entry_i)
        self.trades = []        # open records
        self.closes = []        # {j, reason, est_px, size}
        self.bankrupt = False

    # ── order callbacks ─────────────────────────────────────────────────────

    def notify_order(self, order):
        pass

    # ── helpers ──────────────────────────────────────────────────────────────

    def _num_open(self):
        return len(self.book)

    # ── main loop ───────────────────────────────────────────────────────────

    def next(self):
        i = len(self.datas[0])

        # warmup: donchian (needs [-2]) + macd + atr must be defined
        warmup = max(self.p.donchian_n, self.p.macd_slow + self.p.macd_signal + 10, self.p.atr_period + 5) + 1
        if i <= warmup:
            return

        # ── 0. Liquidation guard ──
        mtm_eq = self.broker.getvalue() / SCALE
        if not self.bankrupt and mtm_eq <= 0:
            self.bankrupt = True
            print(f"  LIQUIDATION {self.datas[0].datetime.datetime(0).date()} "
                  f"mtm_eq=${mtm_eq:,.0f}", flush=True)
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

        # ── 1. Manage open positions (H/L first, pessimistic) ──
        for j in list(self.book.keys()):
            pos = self.book[j]
            d = self.datas[j]
            o, h, l = d.open[0], d.high[0], d.low[0]
            atr = pos["atr"]

            exit_raw, reason = None, None

            # chandelier: raise trail with new highs, stop vs LOW
            if h > pos["peak"]:
                pos["peak"] = h
            ns = pos["peak"] - atr * self.p.chandelier_atr
            if ns > pos["stop"]:
                pos["stop"] = ns
            if l <= pos["stop"]:
                exit_raw, reason = pos["stop"], "chandelier_stop"

            # global breakeven: move stop to ~entry for ALL positions at +be_pct
            if not reason and not pos["be_active"] and h >= pos["entry"] * (1 + self.p.be_pct):
                breakeven = pos["entry"] / (1 - COMMISSION - SLIPPAGE)
                if breakeven > pos["stop"]:
                    pos["stop"] = breakeven
                pos["be_active"] = True

            # partial TP: close tp_ratio at +tp_pct, remainder stop -> breakeven
            if not reason and not pos["partial_taken"] and h >= pos["entry"] * (1 + self.p.tp_pct):
                close_size = pos["size"] * self.p.tp_ratio
                if close_size > 0:
                    self._close(j, close_size, "partial_tp", pos["entry"] * (1 + self.p.tp_pct))
                    pos["size"] -= close_size
                    breakeven = pos["entry"] / (1 - COMMISSION - SLIPPAGE)
                    if breakeven > pos["stop"]:
                        pos["stop"] = breakeven
                    pos["partial_taken"] = True

            # second TP for remainder (full exit)
            if not reason and pos["partial_taken"] and h >= pos["entry"] * (1 + self.p.tp2_pct):
                exit_raw, reason = pos["entry"] * (1 + self.p.tp2_pct), "take_profit2"

            # time exit
            if not reason and i - pos["entry_i"] >= self.p.max_hold_days:
                exit_raw, reason = o, "time_exit"

            if reason:
                self._close(j, pos["size"], reason, exit_raw)
                del self.book[j]

        # ── 2. Rank candidates from the PREVIOUS bar close ──
        ranked = []
        for j in range(len(self.datas)):
            # donchian[-2]: the N-day high EXCLUDING the signal bar itself
            # (a close can't break out above its own bar's high) — mirrors
            # honest_backtest_macd_donchian.donchian_high() which drops the
            # current bar from the max window.
            dc = self.donchian[j][-2]
            hist = self.macd[j].macd[-1] - self.macd[j].signal[-1]
            close = self.bref[j][-1]
            atr_v = self.atr[j][-1]
            if math.isnan(dc) or math.isnan(hist) or math.isnan(atr_v) or dc <= 0 or atr_v <= 0:
                continue
            breakout = close > dc
            macd_pos = hist > 0
            if breakout and macd_pos:
                ranked.append({"j": j, "strength": (close / dc - 1) * 100, "atr": atr_v})

        ranked.sort(key=lambda x: x["strength"], reverse=True)
        targets = ranked[: self.p.top_k]

        # ── 3. Rotation: close open positions not in the target set ──
        target_set = {t["j"] for t in targets}
        for j in list(self.book.keys()):
            if j in target_set:
                continue
            pos = self.book[j]
            d = self.datas[j]
            exit_raw = d.open[0]
            # if breakeven already active, floor at breakeven (never lose)
            if (pos["partial_taken"] or pos["be_active"]) and self.p.tp_pct > 0:
                breakeven = pos["entry"] / (1 - COMMISSION - SLIPPAGE)
                if exit_raw < breakeven:
                    exit_raw = breakeven
            self._close(j, pos["size"], "rotation_exit", exit_raw)
            del self.book[j]

        # ── 4. Open new positions at TODAY open ──
        for t in targets:
            j = t["j"]
            if j in self.book:
                continue
            d = self.datas[j]
            price = d.open[0]
            atr_v = t["atr"]
            stop_dist = atr_v * self.p.chandelier_atr
            if stop_dist <= 0 or price <= 0:
                continue

            # risk-based sizing (identical to live bot)
            equity = self.broker.getvalue() / SCALE
            atr_pct = atr_v / price
            lev = max(1.0, min(self.p.max_leverage, 1.0 / (atr_pct * 2)))
            risk_usd = equity * self.p.risk_per_trade
            notional = risk_usd / (stop_dist / price)
            margin = notional / lev if lev > 0 else notional
            max_margin = equity * self.p.allocation_pct
            if margin > max_margin:
                margin = max_margin
                notional = margin * lev
            size = notional / price * SCALE
            if size <= 0:
                continue

            self.book[j] = {
                "side": "long", "size": size, "entry": price,
                "stop": price - stop_dist, "peak": price, "atr": atr_v,
                "be_active": False, "partial_taken": False, "entry_i": i,
            }
            self.buy(data=d, size=size)
            self.trades.append({
                "j": j, "name": d._name, "side": "long", "entry": price,
                "entry_i": i, "reason": "open", "size": size,
            })
            if self.p.verbose:
                print(f"  OPEN {d._name:12} long @ {price:.2f} stop={price-stop_dist:.2f} "
                      f"sz={size/SCALE:.4f} atr={atr_v:.2f} str={t['strength']:.1f}%")

    # ── execution helper ────────────────────────────────────────────────────

    def _close(self, j, size, reason, est_px):
        """Send a market order to close `size`. Actual fill happens at the next
        open; est_px only used for the report."""
        if size <= 0:
            return
        pos = self.book.get(j)
        if pos is None:
            return
        bsize = abs(self.getposition(self.datas[j]).size)
        if bsize <= 0:
            return
        size = min(size, bsize)
        (self.sell if pos["side"] == "long" else self.buy)(data=self.datas[j], size=size)
        self.closes.append({"j": j, "reason": reason, "est_px": est_px, "size": size})
        pos["size"] -= size


class EquityCurve(bt.Analyzer):
    def start(self):
        self.vals = []

    def next(self):
        self.vals.append((self.strategy.datetime.datetime(0), self.strategy.broker.getvalue()))

    def get_analysis(self):
        return {"curve": self.vals}


# ── runner ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="MACD+Donchian Validation — Backtrader")
    ap.add_argument("--pairs", default="BTC,ETH,BNB,XRP,SOL,DOGE,ADA,TRX,AVAX,LTC",
                    help="comma-separated coins")
    ap.add_argument("--days", type=int, default=1100, help="backtest window (daily bars)")
    ap.add_argument("--capital", type=float, default=10000.0)
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--set", nargs="*", default=[],
                    help="param overrides, e.g. --set top_k=4 donchian_n=15")
    p = ap.parse_args()

    overrides = {}
    for s in p.set:
        k, v = s.split("=", 1)
        if v.lower() in ("true", "false"):
            overrides[k] = v.lower() == "true"
        elif v.lstrip("-").isdigit():
            overrides[k] = int(v)
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
    cerebro.addstrategy(MacdDonchian, verbose=p.verbose, **overrides)
    for inst, df in raw.items():
        cerebro.adddata(bt_okx.as_bt_feed(df, name=inst), name=inst)
    cerebro.broker.setcash(p.capital * SCALE)
    cerebro.broker.set_checksubmit(False)
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
    if strat.bankrupt:
        end = 0.0
    curve = [(ts, max(0.0, v / SCALE)) for ts, v in strat.analyzers.eq.get_analysis()["curve"]]
    years = (curve[-1][0] - curve[0][0]).days / 365.25 if len(curve) > 1 else 0.0
    total_ret = (end / start - 1) * 100 if end > 0 else -100.0
    cagr = ((end / start) ** (1 / years) - 1) * 100 if years > 0 and end > 0 else 0.0
    ta = strat.analyzers.trades.get_analysis()

    eq = np.array([v for _, v in curve], dtype=float)
    mask = eq[:-1] > 0
    rets = np.zeros_like(eq[:-1], dtype=float)
    np.divide(np.diff(eq), eq[:-1], out=rets, where=mask)
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
    print("  MACD+DONCHIAN VALIDATION — BACKTRADER (реальные OKX свечи)")
    print("=" * 64)
    print(f"  Данные:        {curve[0][0].date()} -> {curve[-1][0].date()} ({years:.2f} года)")
    if strat.bankrupt:
        print(f"  СТАТУС:        ЛИКВИДАЦИЯ (equity <= 0)")
    print(f"  Капитал:       ${start:,.0f} -> ${end:,.2f}")
    print(f"  Total return:  {total_ret:+.2f}%")
    print(f"  CAGR:          {cagr:+.2f}%")
    print(f"  Sharpe (ann):  {sharpe:.2f}")
    print(f"  Max drawdown:  {max_dd:.2f}%")
    print(f"  Сделок закрыто: {closed_total}  (win {won} / loss {lost})")
    if open_end:
        print(f"  Открыто на конец: " + ", ".join(f"{n} {s:+.4f}" for n, s in open_end))
    if ta.pnl:
        print(f"  Net PnL (реализ.): ${ta.pnl.net.total/SCALE:,.0f}  "
              f"(avg win ${ta.won.pnl.average/SCALE:.1f} / "
              f"avg loss ${ta.lost.pnl.average/SCALE:.1f})")
    print(f"  Записей входа: {sum(1 for t in strat.trades if t['reason']=='open')}")
    by_reason = {}
    for c in strat.closes:
        by_reason[c["reason"]] = by_reason.get(c["reason"], 0) + 1
    print("  Причины выходов:")
    for r, n in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"    {r:16s} {n}")

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
            print(f"    {y}:  банкротство (-> $0){tag}")
    print("=" * 64)
    print("  Вход/выход исполняются по открытию следующего бара; стоп")
    print("  срабатывает на текущем баре, закрытие — на следующем open.")
    print("  Комиссия 0.1%/сторона + проскальзывание 0.05%; OKX-SWAP свечи,")
    print("  сигнал по закрытию предыдущего бара (без подглядывания).")


if __name__ == "__main__":
    main()

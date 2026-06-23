"""
AI Orchestrator — The brain of the trading system.

This replaces Claude from Humbled Trader's setup with our AI capabilities.
It reads rules.json, evaluates market data, and makes trading decisions.

Flow:
1. Scanner finds significant movers
2. Orchestrator fetches candle data for watchlist
3. Computes indicators per rules.json
4. Evaluates entry/exit rules
5. Makes trade decisions (with reasoning)
6. Executes via OKX API
7. Tracks R-multiples
8. Sends Telegram alerts
"""
import json
import time
import asyncio
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

RULES_PATH = Path(__file__).parent / "rules.json"


def load_rules():
    with open(RULES_PATH) as f:
        return json.load(f)


# ── Indicator functions ──

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)

def calc_atr(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def adx(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    up_move = h - h.shift(1)
    down_move = l.shift(1) - l
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    atr_val = tr.ewm(alpha=1/n, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atr_val
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atr_val
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1/n, adjust=False).mean()
    return adx_val, plus_di, minus_di


def compute_indicators(df, rules):
    """Compute all indicators needed by the rules."""
    df = df.copy()

    # Extract indicator params from rules
    entry_rules = rules.get("entry_rules", {}).get("rules", [])
    exit_rules = rules.get("exit_rules", {})

    # Get unique periods needed
    ema_periods = set()
    rsi_periods = set()
    atr_periods = set()
    adx_periods = set()

    for rule in entry_rules:
        ind = rule.get("indicator", "")
        params = rule.get("params", {})
        if ind == "ema":
            ema_periods.add(params.get("fast", 20))
            ema_periods.add(params.get("slow", 50))
        elif ind == "rsi":
            rsi_periods.add(params.get("period", 14))
        elif ind == "adx":
            adx_periods.add(params.get("period", 14))

    sl = exit_rules.get("stop_loss", {})
    tp = exit_rules.get("take_profit", {})
    atr_periods.add(sl.get("atr_period", 14))
    atr_periods.add(tp.get("atr_period", 14))

    # Compute
    for p in ema_periods:
        df[f"EMA_{p}"] = ema(df["Close"], p)
    for p in rsi_periods:
        df[f"RSI_{p}"] = rsi(df["Close"], p)
    for p in atr_periods:
        df[f"ATR_{p}"] = calc_atr(df, p)
    for p in adx_periods:
        adx_val, plus_di, minus_di = adx(df, p)
        df[f"ADX_{p}"] = adx_val
        df[f"PLUS_DI_{p}"] = plus_di
        df[f"MINUS_DI_{p}"] = minus_di

    # Volume
    df["Vol_SMA_20"] = df["Volume"].rolling(20).mean()
    df["Vol_Ratio"] = df["Volume"] / df["Vol_SMA_20"].replace(0, np.nan)

    return df


def evaluate_rules(df, idx, rules, side="entry"):
    """Evaluate all rules at a given index. Returns (pass, details)."""
    if side == "entry":
        rule_block = rules.get("entry_rules", {})
    else:
        return True, {}

    rule_list = rule_block.get("rules", [])
    logic = rule_block.get("logic", "ALL")

    results = {}
    for rule in rule_list:
        rule_id = rule["id"]
        indicator = rule["indicator"]
        params = rule.get("params", {})
        condition = rule["condition"]
        required = rule.get("required", True)

        passed = _evaluate_condition(df, idx, indicator, params, condition)
        results[rule_id] = {
            "passed": passed,
            "required": required,
            "description": rule.get("description", ""),
        }

    # Logic
    if logic == "ALL":
        required_results = [r for r in results.values() if r["required"]]
        all_required_pass = all(r["passed"] for r in required_results)
        return all_required_pass, results
    elif logic == "ANY":
        return any(r["passed"] for r in results.values()), results
    else:
        return all(r["passed"] for r in results.values() if r["required"]), results


def _evaluate_condition(df, idx, indicator, params, condition):
    """Evaluate a single indicator condition."""
    row = df.iloc[idx]
    prev = df.iloc[idx - 1] if idx > 0 else row

    if indicator == "ema":
        fast_p = params.get("fast", 20)
        slow_p = params.get("slow", 50)
        fast_col = f"EMA_{fast_p}"
        slow_col = f"EMA_{slow_p}"
        if fast_col not in df.columns or slow_col not in df.columns:
            return False
        return prev[fast_col] > prev[slow_col]

    elif indicator == "price_distance_ema":
        ema_p = params.get("ema_period", 20)
        max_dist = params.get("max_distance_pct", 2.5)
        min_dist = params.get("min_distance_pct", -3.0)
        ema_col = f"EMA_{ema_p}"
        if ema_col not in df.columns:
            return False
        dist = (prev["Close"] - prev[ema_col]) / prev[ema_col] * 100
        return min_dist < dist < max_dist

    elif indicator == "rsi":
        period = params.get("period", 14)
        rsi_col = f"RSI_{period}"
        if rsi_col not in df.columns:
            return False
        # Parse condition like "rsi > 30"
        parts = condition.replace("rsi", "").strip().split()
        if len(parts) == 2:
            op, val = parts
            val = float(val)
            if op == ">":
                return prev[rsi_col] > val
            elif op == "<":
                return prev[rsi_col] < val
            elif op == ">=":
                return prev[rsi_col] >= val
        return prev[rsi_col] > 30  # default

    elif indicator == "adx":
        period = params.get("period", 14)
        threshold = params.get("threshold", 18)
        adx_col = f"ADX_{period}"
        if adx_col not in df.columns:
            return False
        return prev[adx_col] > threshold and not np.isnan(prev[adx_col])

    elif indicator == "volume_ratio":
        min_ratio = params.get("min_ratio", 1.2)
        vol_ok = prev["Vol_Ratio"] > min_ratio and not np.isnan(prev["Vol_Ratio"])
        # Check OR condition with trend_strong
        fast_p = params.get("fast", 20)
        ema_col = f"EMA_{fast_p}"
        trend_strong = False
        if ema_col in df.columns and f"RSI_14" in df.columns:
            trend_strong = prev[ema_col] > prev.get(f"EMA_{params.get('slow', 50)}", 0) and prev.get("RSI_14", 50) > 50
        return vol_ok or trend_strong

    return False


def detect_regime(df, idx):
    """Detect market regime: bull / bear / sideways."""
    row = df.iloc[idx]
    ema20 = row.get("EMA_20", 0)
    ema50 = row.get("EMA_50", 0)
    adx = row.get("ADX_14", 0)
    rsi = row.get("RSI_14", 50)

    if np.isnan(ema20) or np.isnan(ema50) or np.isnan(adx):
        return "unknown", {}

    ema_bull = ema20 > ema50
    adx_trending = adx > 20 and not np.isnan(adx)

    if ema_bull and adx_trending and rsi > 50:
        regime = "bull"
    elif not ema_bull and adx_trending and rsi < 50:
        regime = "bear"
    else:
        regime = "sideways"

    return regime, {
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "adx": round(adx, 1),
        "rsi": round(rsi, 1),
        "ema_bull": ema_bull,
        "adx_trending": adx_trending,
    }


def generate_planned_trade(df, idx, rules, symbol):
    """Generate a planned trade with full reasoning (AI analysis)."""
    row = df.iloc[idx]
    prev = df.iloc[idx - 1] if idx > 0 else row

    entry_rules = rules.get("entry_rules", {})
    exit_rules = rules.get("exit_rules", {})

    # Regime detection
    regime, regime_info = detect_regime(df, idx)

    # Evaluate all rules
    passed, details = evaluate_rules(df, idx, rules, "entry")

    # Build reasoning
    reasons = []
    for rule_id, detail in details.items():
        status = "✅" if detail["passed"] else "❌"
        reasons.append(f"{status} {detail['description']}")

    # Compute entry zone
    entry_price = row["Close"]
    atr_period = exit_rules.get("stop_loss", {}).get("atr_period", 14)
    atr_col = f"ATR_{atr_period}"
    atr_val = prev.get(atr_col, 0) if atr_col in df.columns else 0

    sl_mult = exit_rules.get("stop_loss", {}).get("atr_multiplier", 1.8)
    tp_mult = exit_rules.get("take_profit", {}).get("atr_multiplier", 4.5)

    stop = entry_price - sl_mult * atr_val if atr_val > 0 else entry_price * 0.92
    target = entry_price + tp_mult * atr_val if atr_val > 0 else entry_price * 1.18

    # R-value
    r_value = entry_price - stop

    # Regime filter — block trades in bad regimes
    regime_ok = regime in ("bull", "unknown")
    regime_block = not regime_ok and passed

    # Determine action
    if regime_block:
        action = "BLOCKED"
        failed = [r for r, d in details.items() if not d["passed"] and d["required"]]
        reasoning = f"<b>BLOCKED</b> — {symbol}\n\n"
        reasoning += f"⚠️ Regime: <b>{regime.upper()}</b> (trading only in BULL)\n"
        reasoning += f"Failed rules: {', '.join(failed) if failed else 'none (regime filter)'}\n"
    elif passed:
        action = "LONG"
        reasoning = f"<b>BUY SIGNAL</b> — {symbol}\n\n"
    else:
        failed = [r for r, d in details.items() if not d["passed"] and d["required"]]
        action = "WAIT"
        reasoning = f"<b>NO SIGNAL</b> — {symbol}\n\n"
        reasoning += f"Failed rules: {', '.join(failed)}\n"

    reasoning += "Rule Evaluation:\n" + "\n".join(reasons)
    reasoning += f"\n\n📊 Technical:\n"
    reasoning += f"  Price: ${entry_price:,.2f}\n"
    reasoning += f"  ATR(14): ${atr_val:,.2f}\n"
    reasoning += f"  Stop: ${stop:,.2f} ({sl_mult}x ATR)\n"
    reasoning += f"  Target: ${target:,.2f} ({tp_mult}x ATR)\n"
    reasoning += f"  R:R = 1:{tp_mult/sl_mult:.1f}\n"

    # Regime context
    regime_emoji = {"bull": "🟢", "bear": "🔴", "sideways": "🟡", "unknown": "⚪"}
    reasoning += f"\n  {regime_emoji.get(regime, '⚪')} Regime: <b>{regime.upper()}</b>\n"
    if regime_info:
        reasoning += f"  EMA20: ${regime_info['ema20']:,.2f} | EMA50: ${regime_info['ema50']:,.2f}\n"
        reasoning += f"  ADX: {regime_info['adx']} | RSI: {regime_info['rsi']}\n"
    reasoning += f"  Volume: {prev.get('Vol_Ratio', 0):.2f}x avg\n"

    return {
        "symbol": symbol,
        "action": action,
        "entry_price": round(entry_price, 2),
        "entry_zone": [round(stop + r_value * 0.3, 2), round(entry_price, 2)],
        "stop_loss": round(stop, 2),
        "target_price": round(target, 2),
        "r_value": round(r_value, 2),
        "reasoning": reasoning,
        "rules_passed": passed and not regime_block,
        "regime": regime,
        "regime_info": regime_info,
        "details": details,
        "timestamp": datetime.now().isoformat(),
    }


class Orchestrator:
    """
    AI Orchestrator — reads rules.json, evaluates market, executes trades.
    """

    def __init__(self, client_manager=None, trade_log=None):
        self.client_manager = client_manager
        self.trade_log = trade_log or []
        self.rules = load_rules()
        self.last_scan = []
        self.last_evaluations = {}
        self.cycle_count = 0

    async def fetch_candles(self, inst_id, timeframe="4H", limit=100):
        """Fetch candles from OKX."""
        if not self.client_manager:
            print("[ORCH] No client manager", flush=True)
            return None

        client = self.client_manager.get_client()
        if not client:
            print("[ORCH] No OKX client", flush=True)
            return None

        try:
            candles = await client.get_candles(instId=inst_id, bar=timeframe, limit=str(limit))
            if not candles:
                return None

            rows = []
            for c in reversed(candles):  # OKX returns newest first
                rows.append({
                    "timestamp": int(c[0]),
                    "Open": float(c[1]),
                    "High": float(c[2]),
                    "Low": float(c[3]),
                    "Close": float(c[4]),
                    "Volume": float(c[5]),
                })
            df = pd.DataFrame(rows)
            df.index = pd.to_datetime(df["timestamp"], unit="ms")
            return df
        except Exception as e:
            print(f"[ORCH] Error fetching {inst_id}: {e}", flush=True)
            return None

    async def evaluate_symbol(self, symbol):
        """Full evaluation of a single symbol."""
        timeframe = self.rules.get("timeframe", "4H")
        df = await self.fetch_candles(symbol, timeframe, 100)
        if df is None or len(df) < 60:
            return None

        df = compute_indicators(df, self.rules)
        idx = len(df) - 1
        planned = generate_planned_trade(df, idx, self.rules, symbol)
        self.last_evaluations[symbol] = planned
        return planned

    async def run_cycle(self, watchlist=None):
        """Run one complete evaluation cycle."""
        self.cycle_count += 1
        print(f"\n{'='*60}", flush=True)
        print(f"[ORCH] Cycle #{self.cycle_count} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        print(f"{'='*60}", flush=True)

        # Merge: configured symbols + scanner results
        if watchlist is None:
            configured = self.rules.get("symbols", [])
            scanner_symbols = [r["instId"] for r in self.last_scan]
            watchlist = list(dict.fromkeys(configured + scanner_symbols))

        results = []
        for symbol in watchlist:
            planned = await self.evaluate_symbol(symbol)
            if planned:
                results.append(planned)
                action_emoji = "🟢" if planned["action"] == "LONG" else "🔴" if planned["action"] == "BLOCKED" else "⏳"
                regime = planned.get("regime", "?")
                print(f"  {action_emoji} {symbol}: {planned['action']} "
                      f"@ ${planned['entry_price']:,.2f} "
                      f"[{regime}] "
                      f"(rules {'PASSED' if planned['rules_passed'] else 'FAILED'})", flush=True)
            await asyncio.sleep(0.5)  # Rate limiting

        print(f"\n[ORCH] Evaluated {len(results)} symbols, "
              f"{sum(1 for r in results if r['rules_passed'])} with signals", flush=True)

        return results

    def get_planned_trades(self):
        """Get all last evaluations."""
        return self.last_evaluations

    def get_status(self):
        """Get orchestrator status."""
        return {
            "cycle_count": self.cycle_count,
            "strategy": self.rules.get("name", "Unknown"),
            "timeframe": self.rules.get("timeframe", "?"),
            "symbols": self.rules.get("symbols", []),
            "last_evaluations": len(self.last_evaluations),
            "active_signals": sum(1 for e in self.last_evaluations.values() if e.get("rules_passed")),
        }


# CLI entry point
if __name__ == "__main__":
    import asyncio

    async def main():
        orch = Orchestrator()
        print("AI Orchestrator — standalone test\n")
        print(f"Strategy: {orch.rules.get('name')}")
        print(f"Timeframe: {orch.rules.get('timeframe')}")
        print(f"Symbols: {orch.rules.get('symbols')}")
        print(f"\nRunning evaluation cycle...")
        results = await orch.run_cycle()
        for r in results:
            print(f"\n{'='*40}")
            print(r["reasoning"])

    asyncio.run(main())

"""
R-Multiple Tracker — Tracks per-trade R multiples and performance metrics.
Every trade is measured in R (risk units): R = (entry - stop).
"""
import json
import time
from pathlib import Path
from datetime import datetime

DATA_PATH = Path(__file__).parent / "data"
TRADES_FILE = DATA_PATH / "r_trades.json"
DAILY_FILE = DATA_PATH / "r_daily.json"


def ensure_dirs():
    DATA_PATH.mkdir(parents=True, exist_ok=True)


def _load_json(path, default=None):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default if default is not None else []


def _save_json(path, data):
    ensure_dirs()
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


class RTracker:
    def __init__(self):
        ensure_dirs()
        self.trades = _load_json(TRADES_FILE, [])
        self.daily = _load_json(DAILY_FILE, {})

    def open_trade(self, symbol, entry_price, stop_loss, size, side="long",
                   signal_type=None, metadata=None):
        """Record trade entry with R calculation."""
        r_value = abs(entry_price - stop_loss)
        trade = {
            "id": f"{symbol}_{int(time.time())}",
            "symbol": symbol,
            "side": side,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "size": size,
            "r_value": r_value,
            "entry_time": datetime.now().isoformat(),
            "status": "open",
            "peak_r": 0.0,
            "current_r": 0.0,
            "signal_type": signal_type,
            "metadata": metadata or {},
        }
        self.trades.append(trade)
        self._save()
        return trade

    def update_price(self, trade_id, current_price):
        """Update R-multiple based on current price."""
        for trade in self.trades:
            if trade["id"] == trade_id and trade["status"] == "open":
                r_value = trade["r_value"]
                if r_value <= 0:
                    return trade
                if trade["side"] == "long":
                    current_r = (current_price - trade["entry_price"]) / r_value
                else:
                    current_r = (trade["entry_price"] - current_price) / r_value
                trade["current_r"] = round(current_r, 3)
                trade["peak_r"] = round(max(trade["peak_r"], current_r), 3)
                trade["last_price"] = current_price
                trade["last_update"] = datetime.now().isoformat()
                self._save()
                return trade
        return None

    def close_trade(self, trade_id, exit_price, exit_reason="manual"):
        """Close trade and record final R-multiple."""
        for trade in self.trades:
            if trade["id"] == trade_id and trade["status"] == "open":
                r_value = trade["r_value"]
                if trade["side"] == "long":
                    final_r = (exit_price - trade["entry_price"]) / r_value
                else:
                    final_r = (trade["entry_price"] - exit_price) / r_value

                trade["exit_price"] = exit_price
                trade["exit_time"] = datetime.now().isoformat()
                trade["final_r"] = round(final_r, 3)
                trade["exit_reason"] = exit_reason
                trade["status"] = "closed"

                # PnL in USD
                if trade["side"] == "long":
                    trade["pnl_usd"] = round((exit_price - trade["entry_price"]) * trade["size"], 2)
                else:
                    trade["pnl_usd"] = round((trade["entry_price"] - exit_price) * trade["size"], 2)

                # Update daily stats
                day = trade["exit_time"][:10]
                if day not in self.daily:
                    self.daily[day] = {"trades": 0, "wins": 0, "total_r": 0, "total_pnl": 0}
                self.daily[day]["trades"] += 1
                if final_r > 0:
                    self.daily[day]["wins"] += 1
                self.daily[day]["total_r"] = round(self.daily[day]["total_r"] + final_r, 3)
                self.daily[day]["total_pnl"] = round(self.daily[day]["total_pnl"] + trade["pnl_usd"], 2)

                self._save()
                return trade
        return None

    def get_open_trades(self):
        return [t for t in self.trades if t["status"] == "open"]

    def get_closed_trades(self):
        return [t for t in self.trades if t["status"] == "closed"]

    def get_stats(self):
        """Calculate overall performance stats."""
        closed = self.get_closed_trades()
        if not closed:
            return {
                "total_trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0, "avg_r": 0, "total_r": 0,
                "profit_factor": 0, "max_win_r": 0, "max_loss_r": 0,
                "avg_win_r": 0, "avg_loss_r": 0,
            }

        wins = [t for t in closed if t["final_r"] > 0]
        losses = [t for t in closed if t["final_r"] <= 0]
        total_win_r = sum(t["final_r"] for t in wins)
        total_loss_r = sum(t["final_r"] for t in losses)

        return {
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(closed) * 100, 1),
            "avg_r": round(sum(t["final_r"] for t in closed) / len(closed), 3),
            "total_r": round(sum(t["final_r"] for t in closed), 3),
            "profit_factor": round(abs(total_win_r / total_loss_r), 3) if total_loss_r != 0 else float("inf"),
            "max_win_r": round(max((t["final_r"] for t in closed), default=0), 3),
            "max_loss_r": round(min((t["final_r"] for t in closed), default=0), 3),
            "avg_win_r": round(total_win_r / len(wins), 3) if wins else 0,
            "avg_loss_r": round(total_loss_r / len(losses), 3) if losses else 0,
        }

    def format_trade(self, trade):
        """Format single trade for display."""
        r = trade.get("current_r", trade.get("final_r", 0))
        r_emoji = "🟢" if r > 0 else "🔴" if r < 0 else "⚪"
        status = "OPEN" if trade["status"] == "open" else f"CLOSED ({trade.get('exit_reason', '?')})"
        return (
            f"{r_emoji} {trade['symbol']} {trade['side'].upper()} "
            f"@ ${trade['entry_price']:,.2f} | "
            f"R: {r:+.2f} (peak: {trade.get('peak_r', 0):+.2f}) | "
            f"Stop: ${trade['stop_loss']:,.2f} | "
            f"{status}"
        )

    def format_stats(self):
        """Format stats for display."""
        s = self.get_stats()
        if s["total_trades"] == 0:
            return "No trades yet."
        return (
            f"📊 R-Multiple Stats\n"
            f"  Trades: {s['total_trades']} (W:{s['wins']} L:{s['losses']})\n"
            f"  Win Rate: {s['win_rate']}%\n"
            f"  Avg R: {s['avg_r']:+.3f}\n"
            f"  Total R: {s['total_r']:+.3f}\n"
            f"  Profit Factor: {s['profit_factor']:.2f}\n"
            f"  Max Win: {s['max_win_r']:+.3f}R | Max Loss: {s['max_loss_r']:+.3f}R\n"
            f"  Avg Win: {s['avg_win_r']:+.3f}R | Avg Loss: {s['avg_loss_r']:+.3f}R"
        )

    def format_daily_summary(self, days=7):
        """Format daily performance summary."""
        sorted_days = sorted(self.daily.keys(), reverse=True)[:days]
        if not sorted_days:
            return "No daily data."

        lines = [f"📅 Daily Summary (last {len(sorted_days)} days)\n"]
        for day in sorted_days:
            d = self.daily[day]
            wr = d["wins"] / d["trades"] * 100 if d["trades"] > 0 else 0
            r_emoji = "🟢" if d["total_r"] > 0 else "🔴"
            lines.append(
                f"  {day}: {d['trades']} trades, "
                f"WR {wr:.0f}%, "
                f"R: {d['total_r']:+.2f}, "
                f"PnL: ${d['total_pnl']:+.2f} {r_emoji}"
            )
        return "\n".join(lines)

    def _save(self):
        _save_json(TRADES_FILE, self.trades)
        _save_json(DAILY_FILE, self.daily)


# Singleton
tracker = RTracker()

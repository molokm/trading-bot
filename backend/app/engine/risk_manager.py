from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class RiskState:
    daily_pnl: float = 0.0
    today: str = ""
    consecutive_losses: int = 0
    last_order_ts: float = 0.0


@dataclass
class RiskResult:
    ok: bool = True
    reason: str = ""


class RiskManager:
    def __init__(self):
        self._states: dict[str, RiskState] = {}
        self.max_position_pct = 0.95
        self.max_daily_loss_pct = 0.05
        self.max_consecutive_losses = 5
        self.min_interval_sec = 10
        self.max_active_bots = 10

    def _state(self, bot_id: str) -> RiskState:
        if bot_id not in self._states:
            self._states[bot_id] = RiskState()
        return self._states[bot_id]

    def reset_state(self, bot_id: str):
        self._states.pop(bot_id, None)

    def record_trade(self, bot_id: str, pnl: float):
        st = self._state(bot_id)
        today = date.today().isoformat()
        if st.today != today:
            st.daily_pnl = 0.0
            st.today = today
            st.consecutive_losses = 0
        st.daily_pnl += pnl
        if pnl < 0:
            st.consecutive_losses += 1
        else:
            st.consecutive_losses = 0
        st.last_order_ts = time.time()

    def check_open(self, bot: "LiveBot", signal_type: str, size: float,
                   current_price: float, active_bot_count: int) -> RiskResult:
        if bot.mode == "live":
            if active_bot_count >= self.max_active_bots:
                return RiskResult(False, f"Превышен лимит активных ботов ({self.max_active_bots})")

            cost = size * current_price
            if cost > bot.capital * self.max_position_pct:
                return RiskResult(False, f"Размер позиции {cost:.2f} превышает лимит {bot.capital * self.max_position_pct:.2f}")

            st = self._state(bot.id)
            if st.daily_pnl < -bot.capital * self.max_daily_loss_pct:
                return RiskResult(False, f"Достигнут дневной лимит убытка ({self.max_daily_loss_pct*100:.0f}%)")

            if st.consecutive_losses >= self.max_consecutive_losses:
                return RiskResult(False, f"Достигнут лимит подряд убыточных сделок ({self.max_consecutive_losses})")

            elapsed = time.time() - st.last_order_ts
            if elapsed < self.min_interval_sec:
                return RiskResult(False, f"Слишком частые ордера (прошло {elapsed:.0f}с, минимум {self.min_interval_sec}с)")

        return RiskResult(ok=True)


risk_manager = RiskManager()

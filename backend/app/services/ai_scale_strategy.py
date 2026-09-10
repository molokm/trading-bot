"""AI Scale-In 1H — copy of AI Discretionary with LLM-driven averaging (DCA).

Demo-first: entry on signal as usual; if price moves against the position but
trend indicators still agree with the side, AI may ADD to the position in parts.
Exit remains indicator/LLM driven (close/reduce).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .ai_strategy import (
    AIStrategy, AIConfig, AIPosition, ALLOWED_SYMBOLS,
)
from .ai_agent import call_llm, validate_decision, mock_decide
from .position_claim import claim_open

AI_SCALE_BOT_ID = "ai_scale_strategy"
STRATEGY_NAME = "AI Scale-In 1H"
STRATEGY_VERSION = "v1.1-strict"
STRATEGY_DESC = (
    "Копия AI Discretionary: вход по сигналу, докупки частями если цена против "
    "но тренд по индикаторам сохраняется. Объём входа и add определяет AI."
)


@dataclass
class AIScaleConfig(AIConfig):
    max_positions: int = 1
    # Scale-in (phase4: stricter)
    scale_enabled: bool = True
    max_adds: int = 2                    # was 3 — fewer averages
    min_adverse_pct: float = 0.6         # need clearer pullback
    max_adverse_pct: float = 2.2         # no deep averaging into a trend break
    min_trend_align: float = 0.62        # stronger trend confirmation
    add_size_frac_min: float = 0.20
    add_size_frac_max: float = 0.50      # smaller adds
    max_total_risk_mult: float = 1.7     # was 2.2
    scale_cooldown_sec: int = 1800       # 30m between adds
    require_adx_for_add: float = 18.0    # ADX must still show trend
    block_add_on_funding: bool = True


class AIScaleStrategy(AIStrategy):
    BOT_ID = AI_SCALE_BOT_ID
    BOT_NAME = STRATEGY_NAME
    STRATEGY_NAME = STRATEGY_NAME
    STRATEGY_VERSION = STRATEGY_VERSION
    STRATEGY_DESC = STRATEGY_DESC

    def __init__(self, config: AIScaleConfig = None, **kwargs):
        cfg = config or AIScaleConfig()
        super().__init__(config=cfg, **kwargs)
        self._add_counts: dict[str, int] = {}
        self._last_add_ts: dict[str, float] = {}
        self._initial_size: dict[str, float] = {}

    def _clord_prefix(self) -> str:
        return "ais"

    async def _place(self, client, inst_id, side, sz, pos_side):
        coin = inst_id.split("-")[0]
        cl_id = f"{self._clord_prefix()}{int(time.time() * 1000)}"
        return await client.place_order(
            inst_id=inst_id, side=side, ord_type="market",
            sz=self._fmt_sz(coin, sz), td_mode="cross", pos_side=pos_side,
            cl_ord_id=cl_id,
        )

    def _snapshot(self) -> dict:
        snap = super()._snapshot()
        snap["strategy"] = STRATEGY_NAME
        snap["version"] = STRATEGY_VERSION
        snap["scale_in"] = {
            "enabled": bool(getattr(self.config, "scale_enabled", True)),
            "max_adds": int(getattr(self.config, "max_adds", 3)),
            "min_adverse_pct": float(getattr(self.config, "min_adverse_pct", 0.4)),
            "max_adverse_pct": float(getattr(self.config, "max_adverse_pct", 3.5)),
            "min_trend_align": float(getattr(self.config, "min_trend_align", 0.55)),
            "add_counts": dict(self._add_counts),
            "initial_size": dict(self._initial_size),
        }
        # Enrich open positions with adverse % and trend still-ok
        q = snap.get("quant") or {}
        coins_q = q.get("coins") or {}
        enriched = []
        for p in (snap.get("open_positions") or []):
            coin = p.get("coin")
            side = (p.get("side") or "long").lower()
            entry = float(p.get("entry_price") or 0)
            ind = (self._latest_indicators or {}).get(coin) or {}
            px = float(ind.get("close") or 0)
            adverse = 0.0
            if entry > 0 and px > 0:
                if side == "long":
                    adverse = max(0.0, (entry - px) / entry * 100.0)
                else:
                    adverse = max(0.0, (px - entry) / entry * 100.0)
            cq = coins_q.get(coin) or {}
            align = float(cq.get("align_long") if side == "long" else cq.get("align_short") or 0)
            if align <= 0:
                align = float(cq.get("align_score") or 0)
            trend_ok = align >= float(getattr(self.config, "min_trend_align", 0.55))
            adds = int(self._add_counts.get(coin, 0))
            adx_v = float(ind.get("adx") or 0)
            fr = ind.get("funding_rate")
            try:
                fr = float(fr) if fr is not None else None
            except (TypeError, ValueError):
                fr = None
            funding_ok = True
            if getattr(self.config, "block_add_on_funding", True) and fr is not None:
                lim = float(getattr(self.config, "funding_block_abs", 0.0008) or 0.0008)
                if side == "long" and fr >= lim:
                    funding_ok = False
                if side == "short" and fr <= -lim:
                    funding_ok = False
            adx_need = float(getattr(self.config, "require_adx_for_add", 18) or 0)
            adx_ok = adx_v >= adx_need if adx_need > 0 else True
            # BTC impulse: no add against BTC on alts
            btc_ok = True
            impulse = (q.get("btc_impulse") if q else None)
            if coin != "BTC" and impulse:
                if impulse == "up" and side == "short":
                    btc_ok = False
                if impulse == "down" and side == "long":
                    btc_ok = False
            can_add = (
                bool(getattr(self.config, "scale_enabled", True))
                and adverse >= float(getattr(self.config, "min_adverse_pct", 0.6))
                and adverse <= float(getattr(self.config, "max_adverse_pct", 2.2))
                and trend_ok
                and adx_ok
                and funding_ok
                and btc_ok
                and adds < int(getattr(self.config, "max_adds", 2))
            )
            row = dict(p)
            row.update({
                "adverse_pct": round(adverse, 3),
                "trend_align": round(align, 3),
                "trend_ok": trend_ok,
                "adds_done": adds,
                "can_add": can_add,
            })
            enriched.append(row)
        snap["open_positions"] = enriched
        snap["policy_hint"] = (
            "SCALE-IN MODE: if open position and can_add=true, you MAY action=add "
            "with size_pct 0.25-0.75 of initial size (not full new position). "
            "Prefer add only when trend_ok and adverse is moderate. "
            "Otherwise close/reduce/hold as usual. Never open a second coin while one is open."
        )
        return snap

    async def _apply_decision(self, client, decision: dict):
        """Extend base with action=add."""
        action = (decision.get("action") or "hold").lower()
        if action == "add":
            await self._add(client, decision)
            return
        # Delegate open/close/reduce/hold to parent path via same code as _tick tail
        # Parent has logic inline in _tick — call shared handlers
        if action == "close":
            coin = (decision.get("symbol") or "").upper()
            if coin:
                await self._close(client, coin, reason=decision.get("reason") or "ai_scale_close")
            return
        if action == "reduce":
            coin = (decision.get("symbol") or "").upper()
            if coin and coin in self._positions:
                # reduce half
                await self._reduce(client, coin, frac=0.5, reason=decision.get("reason") or "ai_scale_reduce")
            return
        if action == "open":
            # use parent open path by simulating decision fields
            coin = (decision.get("symbol") or "").upper()
            side = (decision.get("side") or "").lower()
            if not coin or side not in ("long", "short"):
                return
            if self._positions:
                self._record_exec("open_skip", coin=coin, side=side, reason="scale: already in position")
                return
            stop_pct = float(decision.get("stop_pct") or 0.03)
            take_pct = float(decision.get("take_pct") or 0.06)
            stop_pct = min(float(self.config.max_stop_pct), max(float(self.config.min_stop_pct), stop_pct))
            take_pct = max(float(self.config.min_take_pct), take_pct)
            if take_pct < stop_pct * 1.3:
                take_pct = stop_pct * 1.5
            # optional size override from LLM size_pct (0-1 of normal size)
            await self._open(client, coin, side, stop_pct=stop_pct, take_pct=take_pct,
                             reason=decision.get("reason") or "ai_scale_open")
            pos = self._positions.get(coin)
            if pos:
                self._initial_size[coin] = float(pos.size)
                self._add_counts[coin] = 0
            return
        # hold — nothing

    async def _reduce(self, client, coin: str, frac: float = 0.5, reason: str = "reduce"):
        pos = self._positions.get(coin)
        if not pos:
            return
        frac = max(0.1, min(0.9, float(frac)))
        cut = float(pos.size) * frac
        if cut <= 0:
            return
        close_side = "sell" if pos.side == "long" else "buy"
        if not self._execute_enabled():
            print(f"[AI-Scale] SIGNAL reduce {coin} frac={frac} ({reason})", flush=True)
            return
        resp = await self._place(client, pos.inst_id, close_side, cut, pos.side)
        if resp.get("error"):
            self._record_exec("reduce_error", coin=coin, reason=str(resp.get("message")))
            return
        pos.size = max(0.0, float(pos.size) - cut)
        self._record_exec("reduce", coin=coin, reason=reason, size=cut)
        if pos.size <= 1e-12:
            await self._close(client, coin, reason="reduce_flat")

    async def _add(self, client, decision: dict):
        coin = (decision.get("symbol") or "").upper()
        pos = self._positions.get(coin)
        if not pos:
            self._record_exec("add_skip", coin=coin, reason="no_position")
            return
        cfg = self.config
        if not getattr(cfg, "scale_enabled", True):
            return
        adds = int(self._add_counts.get(coin, 0))
        if adds >= int(getattr(cfg, "max_adds", 3)):
            self._record_exec("add_skip", coin=coin, reason="max_adds")
            return
        now = time.time()
        last = float(self._last_add_ts.get(coin, 0))
        if now - last < float(getattr(cfg, "scale_cooldown_sec", 900)):
            self._record_exec("add_skip", coin=coin, reason="cooldown")
            return
        ind = self._latest_indicators.get(coin) or {}
        px = float(ind.get("close") or 0)
        entry = float(pos.entry_price or 0)
        if px <= 0 or entry <= 0:
            return
        if pos.side == "long":
            adverse = (entry - px) / entry * 100.0
        else:
            adverse = (px - entry) / entry * 100.0
        if adverse < float(getattr(cfg, "min_adverse_pct", 0.4)):
            self._record_exec("add_skip", coin=coin, reason=f"adverse_low {adverse:.2f}")
            return
        if adverse > float(getattr(cfg, "max_adverse_pct", 3.5)):
            self._record_exec("add_skip", coin=coin, reason=f"adverse_high {adverse:.2f}")
            return
        # trend still ok
        snap = self._snapshot()
        can = False
        for p in snap.get("open_positions") or []:
            if p.get("coin") == coin and p.get("can_add"):
                can = True
                break
        if not can:
            self._record_exec("add_skip", coin=coin, reason="trend_not_ok")
            return
        base = float(self._initial_size.get(coin) or pos.size)
        frac = float(decision.get("size_pct") or decision.get("add_frac") or 0.4)
        frac = max(float(getattr(cfg, "add_size_frac_min", 0.25)),
                   min(float(getattr(cfg, "add_size_frac_max", 0.75)), frac))
        add_sz = base * frac
        max_total = base * float(getattr(cfg, "max_total_risk_mult", 2.2))
        if float(pos.size) + add_sz > max_total + 1e-9:
            add_sz = max(0.0, max_total - float(pos.size))
        if add_sz <= 0:
            self._record_exec("add_skip", coin=coin, reason="size_cap")
            return
        if not self._execute_enabled():
            print(f"[AI-Scale] SIGNAL add {pos.side} {coin} sz={add_sz:.4f} adverse={adverse:.2f}%", flush=True)
            self._record_exec("add_signal", coin=coin, size=add_sz, adverse=adverse)
            return
        order_side = "buy" if pos.side == "long" else "sell"
        resp = await self._place(client, pos.inst_id, order_side, add_sz, pos.side)
        if resp.get("error"):
            self._record_exec("add_error", coin=coin, reason=str(resp.get("message")))
            return
        # Update average entry
        old_sz = float(pos.size)
        new_sz = old_sz + add_sz
        pos.entry_price = (entry * old_sz + px * add_sz) / new_sz if new_sz else entry
        pos.size = new_sz
        self._add_counts[coin] = adds + 1
        self._last_add_ts[coin] = now
        try:
            await claim_open(self.db, self.BOT_ID, pos.inst_id, pos.side, pos.size, pos.entry_price)
        except Exception:
            pass
        self._record_exec(
            "add", coin=coin, side=pos.side, size=add_sz,
            reason=decision.get("reason") or "ai_scale_add",
            adverse=round(adverse, 3), adds=self._add_counts[coin],
        )
        print(
            f"[AI-Scale] ADD {pos.side} {coin} +{add_sz:.4f} avg={pos.entry_price:.4f} "
            f"adverse={adverse:.2f}% adds={self._add_counts[coin]}",
            flush=True,
        )

    async def _tick(self):
        """Same as AI but decision routing includes scale-in add."""
        client = await self._client()
        if not client:
            return
        if not self._positions:
            await self._restore_open_positions(client)
        for coin, pos in list(self._positions.items()):
            await claim_open(
                self.db, self.BOT_ID, pos.inst_id, pos.side, pos.size, pos.entry_price,
            )
            if coin not in self._initial_size:
                self._initial_size[coin] = float(pos.size)
        await self._fetch_indicators(client)
        await self._manage_stops(client)
        try:
            self._refresh_adaptive()
        except Exception:
            pass
        snap = self._snapshot()
        try:
            decision = await call_llm(snap, provider=self._provider())
        except Exception as e:
            print(f"[AI-Scale] LLM error: {e}", flush=True)
            try:
                decision = mock_decide(snap)
            except Exception:
                decision = {"action": "hold", "reason": "llm_error"}
        # Mark scale-ok for validator
        if isinstance(decision, dict):
            decision = dict(decision)
            decision["_scale_ok"] = True
            open_syms = [p.get("coin") for p in (snap.get("open_positions") or [])]
            decision = validate_decision(decision, open_syms)
            decision["_scale_ok"] = True
        self._last_decision = decision
        self._last_activity = datetime.now(timezone.utc).isoformat()
        try:
            self._decision_log.append({
                "time": self._last_activity,
                **{k: decision.get(k) for k in ("action", "symbol", "side", "confidence", "reason")},
            })
            self._decision_log = self._decision_log[-80:]
        except Exception:
            pass
        print(
            f"[AI-Scale] decision action={decision.get('action')} "
            f"sym={decision.get('symbol')} conf={decision.get('confidence')} "
            f"reason={(decision.get('reason') or '')[:80]}",
            flush=True,
        )
        await self._apply_decision(client, decision)

    def get_status(self) -> dict:
        st = super().get_status()
        st["strategy"] = STRATEGY_NAME
        st["version"] = STRATEGY_VERSION
        st["description"] = STRATEGY_DESC
        st["scale_in"] = {
            "enabled": bool(getattr(self.config, "scale_enabled", True)),
            "max_adds": int(getattr(self.config, "max_adds", 3)),
            "add_counts": dict(self._add_counts),
        }
        st["bot_id"] = self.BOT_ID
        return st

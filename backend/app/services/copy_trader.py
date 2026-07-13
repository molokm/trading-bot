"""Copy-trader service — coordinates parsers, signal extraction, and trade execution."""

import asyncio
import json
import os
import sys
import uuid
import threading
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, asdict

# Allow running as standalone script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.telegram_parser import TelegramParser, TelegramPost
from app.services.youtube_parser import YouTubeParser, YouTubeVideo
from app.services.signal_extractor import extract_signals, TradeSignal, Side

CT_BOT_ID = "copy_trader"


@dataclass
class CopyTradeConfig:
    telegram_channel: str = "falconinvestors"
    youtube_channel: str = "AlexFalcony"
    poll_interval_sec: int = 300  # 5 minutes
    min_confidence: float = 0.25
    max_position_pct: float = 0.10  # 10% of capital per trade
    enabled_coins: list = None  # None = all coins
    auto_execute: bool = False  # True = place real orders
    mode: str = "demo"  # "demo" or "live"

    def __post_init__(self):
        if self.enabled_coins is None:
            self.enabled_coins = ["BTC", "ETH"]


class CopyTrader:
    """Main copy-trading service."""

    def __init__(self, config: CopyTradeConfig, client_manager=None, db=None):
        self.config = config
        self.client_manager = client_manager
        self.db = db
        self.telegram_parser = TelegramParser(config.telegram_channel)
        self.youtube_parser = YouTubeParser(config.youtube_channel)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._seen_posts: set = set()
        self._seen_videos: set = set()
        self._signal_log: list = []
        self._trade_log: list = []

    async def _ensure_bot(self):
        """Ensure the copy_trader bot exists in the DB."""
        if not self.db:
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            if self.db._pg_mode:
                await self.db._execute(
                    "INSERT INTO bots (id, strategy_id, strategy_code, symbol, timeframe, "
                    "capital, params, status, mode, signal_type, created_at, name) "
                    "VALUES ($1, 'copy_trader', 'copy_trader', 'BTC-USDT-SWAP', '5m', "
                    "0, '{}', 'running', $2, 'copy_trader', $3, 'Copy Trader') "
                    "ON CONFLICT (id) DO NOTHING",
                    (CT_BOT_ID, self.config.mode, now),
                )
            else:
                await self.db._execute(
                    "INSERT OR IGNORE INTO bots (id, strategy_id, strategy_code, symbol, timeframe, "
                    "capital, params, status, mode, signal_type, created_at, name) "
                    "VALUES (?, 'copy_trader', 'copy_trader', 'BTC-USDT-SWAP', '5m', "
                    "0, '{}', 'running', ?, 'copy_trader', ?, 'Copy Trader')",
                    (CT_BOT_ID, self.config.mode, now),
                )
        except Exception as e:
            print(f"[CopyTrader] DB ensure_bot error: {e}", flush=True)

    async def _reload_from_db(self):
        """Reload signals and trades from DB so they survive restarts."""
        if not self.db:
            return
        try:
            signals = await self.db.get_signals(bot_id=CT_BOT_ID, limit=500)
            for s in signals:
                if s.get("status") not in ("rejected", "failed"):
                    self._signal_log.append({
                        "side": s.get("side", ""),
                        "coin": "BTC",
                        "confidence": 0.25,
                        "source": "copy_trader",
                        "source_url": "",
                        "entry_price": None,
                        "sl_price": None,
                        "tp_price": None,
                        "timestamp": s.get("timestamp", ""),
                        "is_exit": False,
                    })

            trades = await self.db.get_trades(bot_id=CT_BOT_ID, limit=100)
            for t in trades:
                self._trade_log.append({
                    "time": t.get("timestamp", ""),
                    "side": t.get("side", ""),
                    "symbol": t.get("inst_id", ""),
                    "size": float(t.get("sz", 0) or 0),
                    "ord_id": t.get("ord_id", ""),
                    "signal": {},
                })

            print(f"[CopyTrader] Reloaded from DB: {len(self._signal_log)} signals, {len(self._trade_log)} trades", flush=True)
        except Exception as e:
            print(f"[CopyTrader] DB reload error: {e}", flush=True)

    async def start(self):
        """Start the copy-trader loop in a background thread."""
        if self._running:
            return
        self._running = True
        await self._ensure_bot()
        await self._reload_from_db()
        self._thread = threading.Thread(target=self._run_thread, daemon=True, name="copy-trader")
        self._thread.start()
        print(f"[CopyTrader] Started — monitoring @{self.config.telegram_channel}", flush=True)

    async def stop(self):
        """Stop the copy-trader loop."""
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=10)
        print("[CopyTrader] Stopped", flush=True)

    def _run_thread(self):
        """Background thread: creates its own event loop and runs polling."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._poll_loop())
        except Exception as e:
            print(f"[CopyTrader] Thread error: {e}", flush=True)
        finally:
            self._loop.close()

    async def _poll_loop(self):
        """Main polling loop — runs inside the thread's event loop."""
        while self._running:
            try:
                await self._poll_telegram()
                await self._poll_youtube()
                await asyncio.sleep(self.config.poll_interval_sec)
            except Exception as e:
                print(f"[CopyTrader] Error: {e}", flush=True)
                await asyncio.sleep(60)

    async def _poll_telegram(self):
        """Fetch and process new Telegram posts."""
        try:
            posts = await self.telegram_parser.fetch_posts(limit=10)
            print(f"[CopyTrader] Telegram: fetched {len(posts)} posts", flush=True)
            for post in posts:
                post_id = post.post_url or post.text[:50]
                if post_id in self._seen_posts:
                    continue
                self._seen_posts.add(post_id)

                signals = extract_signals(
                    text=post.text,
                    source="telegram",
                    source_url=post.post_url,
                    timestamp=post.timestamp.isoformat() if post.timestamp else "",
                )
                for sig in signals:
                    await self._process_signal(sig, post=post)
        except Exception as e:
            print(f"[CopyTrader] Telegram error: {e}", flush=True)

    async def _poll_youtube(self):
        """Fetch and process new YouTube videos."""
        try:
            videos = await self.youtube_parser.fetch_recent_videos(limit=5)
            print(f"[CopyTrader] YouTube: fetched {len(videos)} videos (known_ids={len(self.youtube_parser._known_ids)})", flush=True)
            for video in videos:
                if video.video_id in self._seen_videos:
                    print(f"[CopyTrader] YouTube: skip seen {video.video_id} '{video.title[:50]}'", flush=True)
                    continue
                self._seen_videos.add(video.video_id)

                text = f"{video.title}\n{video.description}"

                print(f"[CopyTrader] YouTube: processing '{video.title[:60]}' (desc={len(video.description)} chars)", flush=True)

                signals = extract_signals(
                    text=text,
                    source="youtube",
                    source_url=video.url,
                    timestamp=video.timestamp.isoformat() if video.timestamp else "",
                )
                if signals:
                    print(f"[CopyTrader] YouTube: found {len(signals)} signals from '{video.title[:40]}'", flush=True)
                for sig in signals:
                    await self._process_signal(sig, video=video)
        except Exception as e:
            print(f"[CopyTrader] YouTube error: {type(e).__name__}: {e}", flush=True)

    async def _process_signal(
        self,
        signal: TradeSignal,
        post: Optional[TelegramPost] = None,
        video: Optional[YouTubeVideo] = None,
    ):
        """Process an extracted signal — log, notify, optionally execute."""
        # Filter by confidence
        if signal.confidence < self.config.min_confidence:
            return

        # Filter by coin
        if signal.coin not in self.config.enabled_coins:
            return

        # Skip close signals for now (need position tracking)
        if signal.is_exit:
            print(f"[CopyTrader] CLOSE signal: {signal.coin} from {signal.source}", flush=True)
            self._signal_log.append(asdict(signal))
            await self._save_signal_db(signal)
            return

        # Log the signal
        source_info = ""
        if post:
            source_info = f"TG[{post.views} views]"
        elif video:
            source_info = f"YT[{video.view_count} views]"

        side_str = "LONG" if signal.side == Side.LONG else "SHORT"
        print(
            f"[CopyTrader] SIGNAL: {side_str} {signal.coin} "
            f"conf={signal.confidence:.2f} "
            f"entry={signal.entry_price} sl={signal.sl_price} tp={signal.tp_price} "
            f"from {source_info}",
            flush=True,
        )

        self._signal_log.append(asdict(signal))
        await self._save_signal_db(signal)

        # Execute trade if enabled
        if self.config.auto_execute and self.client_manager:
            await self._execute_trade(signal)

    async def _save_signal_db(self, signal: TradeSignal):
        """Save signal to DB."""
        if not self.db:
            return
        try:
            side_str = "buy" if signal.side == Side.LONG else "sell"
            if signal.is_exit:
                side_str = "close"
            await self.db.save_signal(
                bot_id=CT_BOT_ID,
                timestamp=signal.timestamp or datetime.now(timezone.utc).isoformat(),
                side=side_str,
                price=signal.entry_price,
                status="executed" if self.config.auto_execute else "pending",
            )
        except Exception as e:
            print(f"[CopyTrader] Save signal error: {e}", flush=True)

    async def _execute_trade(self, signal: TradeSignal):
        """Execute a trade on OKX based on the signal."""
        if not self.client_manager:
            return

        client = self.client_manager.get_client()
        if not client:
            print("[CopyTrader] No OKX client available", flush=True)
            return

        side = "buy" if signal.side == Side.LONG else "sell"
        symbol = f"{signal.coin}-USDT-SWAP"

        try:
            # Get current price
            ticker = await client.get_ticker(symbol)
            if ticker.get("error") or not ticker.get("data"):
                return
            price = float(ticker["data"][0]["last"])

            # Calculate position size
            notional = 1000 * self.config.max_position_pct  # $100 default
            ct_val = 0.01 if signal.coin == "BTC" else 0.1
            sz = notional / (ct_val * price)

            result = await client.place_order(
                inst_id=symbol,
                side=side,
                ord_type="market",
                sz=f"{sz:.2f}",
                td_mode="cross",
                pos_side="long" if side == "buy" else "short",
            )

            if not result.get("error"):
                ord_id = result.get("data", [{}])[0].get("ordId", "")
                print(f"[CopyTrader] ORDER PLACED: {side} {symbol} sz={sz:.2f} ord={ord_id}", flush=True)

                trade_entry = {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "side": side,
                    "symbol": symbol,
                    "size": sz,
                    "ord_id": ord_id,
                    "signal": asdict(signal),
                }
                self._trade_log.append(trade_entry)

                # Persist trade to DB
                await self._save_trade_db(trade_entry, signal)
            else:
                print(f"[CopyTrader] ORDER FAILED: {result.get('message', '')}", flush=True)
        except Exception as e:
            print(f"[CopyTrader] Execute error: {e}", flush=True)

    async def _save_trade_db(self, trade: dict, signal: TradeSignal):
        """Save executed trade to DB."""
        if not self.db:
            return
        try:
            await self.db.save_trade(
                bot_id=CT_BOT_ID,
                side=trade["side"],
                sz=f"{trade['size']:.2f}",
                ord_id=trade["ord_id"],
                inst_id=trade["symbol"],
                state="filled",
            )
        except Exception as e:
            print(f"[CopyTrader] Save trade error: {e}", flush=True)

    def get_status(self) -> dict:
        """Get current copy-trader status."""
        return {
            "running": self._running,
            "config": asdict(self.config),
            "signals_seen": len(self._signal_log),
            "trades_executed": len(self._trade_log),
            "recent_signals": self._signal_log[-10:],
            "recent_trades": self._trade_log[-5:],
        }


async def test_copy_trader():
    """Test the copy-trader with Telegram only (no OKX)."""
    config = CopyTradeConfig(
        auto_execute=False,
        min_confidence=0.25,
    )
    trader = CopyTrader(config=config)

    # Run one poll cycle
    print("=== Polling Telegram ===")
    await trader._poll_telegram()
    status = trader.get_status()
    print(f"\nSignals found: {status['signals_seen']}")
    for s in status["recent_signals"]:
        print(f"  {s['side']:6} {s['coin']} conf={s['confidence']:.2f} from={s['source']}")


if __name__ == "__main__":
    asyncio.run(test_copy_trader())

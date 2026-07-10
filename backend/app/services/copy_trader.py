"""Copy-trader service — coordinates parsers, signal extraction, and trade execution."""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, asdict

# Allow running as standalone script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.telegram_parser import TelegramParser, TelegramPost
from app.services.youtube_parser import YouTubeParser, YouTubeVideo
from app.services.signal_extractor import extract_signals, TradeSignal, Side


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
        self._task: Optional[asyncio.Task] = None
        self._seen_posts: set = set()
        self._seen_videos: set = set()
        self._signal_log: list = []
        self._trade_log: list = []

    async def start(self):
        """Start the copy-trader loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        print(f"[CopyTrader] Started — monitoring @{self.config.telegram_channel}", flush=True)

    async def stop(self):
        """Stop the copy-trader loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        print("[CopyTrader] Stopped", flush=True)

    async def _loop(self):
        """Main polling loop."""
        while self._running:
            try:
                await self._poll_telegram()
                await self._poll_youtube()
                await asyncio.sleep(self.config.poll_interval_sec)
            except asyncio.CancelledError:
                break
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

        # Execute trade if enabled
        if self.config.auto_execute and self.client_manager:
            await self._execute_trade(signal)

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
                self._trade_log.append({
                    "time": datetime.now(timezone.utc).isoformat(),
                    "side": side,
                    "symbol": symbol,
                    "size": sz,
                    "ord_id": ord_id,
                    "signal": asdict(signal),
                })
            else:
                print(f"[CopyTrader] ORDER FAILED: {result.get('message', '')}", flush=True)
        except Exception as e:
            print(f"[CopyTrader] Execute error: {e}", flush=True)

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

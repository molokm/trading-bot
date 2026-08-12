"""EquityTracker — periodic equity snapshots for the public /tracker page.

Writes one row into performance_metrics (bot_id='portfolio') every interval so
the public tracker can render an equity curve. Runs in a daemon thread with its
own event loop, like the strategies.
"""

import asyncio
import logging
import os
import threading
from typing import Optional

log = logging.getLogger("equity_tracker")

SNAPSHOT_INTERVAL = int(os.getenv("TRACKER_INTERVAL_SEC", "600"))


class EquityTracker:
    def __init__(self, client_manager=None, db=None):
        self.client_manager = client_manager
        self.db = db
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def snapshot_once(self) -> bool:
        """Write one equity snapshot for the owner portfolio. Returns True on success."""
        try:
            client = self.client_manager.get_client() if self.client_manager else None
            if not client or not self.db:
                return False
            result = await client.get_balance()
            if result.get("error"):
                return False
            data = result.get("data", [])
            if not data:
                return False
            acct = data[0] if isinstance(data, list) else data
            eq = float(acct.get("totalEq", 0) or 0)
            if eq <= 0:
                return False
            await self.db.ensure_bot("portfolio", name="Portfolio")
            await self.db.save_metric(bot_id="portfolio", equity=eq, total_pnl=0)
            return True
        except Exception as e:
            log.warning("snapshot error: %s", e)
            return False

    async def _poll_loop(self):
        while self._running:
            try:
                await self.snapshot_once()
            except Exception as e:
                log.warning("poll error: %s", e)
            await asyncio.sleep(SNAPSHOT_INTERVAL)

    def _thread_runner(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._poll_loop())
        except RuntimeError:
            if self._running:
                log.warning("equity tracker loop stopped unexpectedly")
        except Exception as e:
            log.warning("equity tracker thread error: %s", e)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._thread_runner, daemon=True)
        self._thread.start()
        log.info("Equity tracker started (interval=%ss)", SNAPSHOT_INTERVAL)

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._loop and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        log.info("Equity tracker stopped")

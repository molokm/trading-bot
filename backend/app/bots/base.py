"""BaseBot — shared utilities for all trading bots.

Provides common infrastructure:
- Execution logging
- State persistence
- Risk guard integration
- Client manager access
"""
from __future__ import annotations

import os
import time
import json
from datetime import datetime, timezone
from typing import Optional, Any


def _bot_state_path(bot_name: str) -> str:
    """Get persistent state file path for a bot."""
    candidates = [
        os.getenv("DATA_DIR"),
        os.getenv("RENDER_DISK_PATH"),
        "/var/data",
        os.path.join(os.getcwd(), "data"),
        "/tmp",
    ]
    for base in candidates:
        if not base:
            continue
        try:
            os.makedirs(base, exist_ok=True)
            probe = os.path.join(base, ".wprobe")
            with open(probe, "w") as f:
                f.write("1")
            os.remove(probe)
            return os.path.join(base, f"{bot_name}_state.json")
        except Exception:
            continue
    return os.path.join("/tmp", f"{bot_name}_state.json")


def load_state(bot_name: str, default: dict = None) -> dict:
    """Load bot state from disk."""
    path = _bot_state_path(bot_name)
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default or {}


def save_state(bot_name: str, state: dict) -> None:
    """Save bot state to disk."""
    path = _bot_state_path(bot_name)
    try:
        with open(path, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def exec_evt(kind: str, coin: str = "", side: str = "", reason: str = "", **extra) -> dict:
    """Create a structured execution log entry."""
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "coin": coin,
        "side": side,
        "reason": reason,
    }
    if extra:
        row.update(extra)
    return row


class BaseBot:
    """Base class for all trading bots."""

    bot_name: str = "base"

    def __init__(
        self,
        client_manager: Any = None,
        live_client_manager: Any = None,
        db: Any = None,
        notifier: Any = None,
        **kwargs: Any,
    ):
        self.client_manager = client_manager
        self.live_client_manager = live_client_manager
        self.db = db
        self.notifier = notifier
        self._running = False
        self._start_time: Optional[float] = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Start the bot."""
        self._running = True
        self._start_time = time.time()

    def stop(self) -> None:
        """Stop the bot."""
        self._running = False

    def status(self) -> dict:
        """Return bot status."""
        return {
            "running": self._running,
            "bot_name": self.bot_name,
            "uptime_sec": time.time() - self._start_time if self._start_time else 0,
        }

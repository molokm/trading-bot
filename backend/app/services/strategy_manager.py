"""StrategyManager — per-user (multi-tenant) trading bot instances.

Each Telegram user with a Pro plan can start their own Momentum Rotation and
Impulse 1D strategies on THEIR OWN OKX account. Strategies are keyed by
telegram_id; the owner (admin) keeps using the global instances with env creds.

Per-user strategy instances use a suffixed bot_id (e.g. "rotation_strategy:<uid>")
so trades/positions/PnL stay isolated per user in the shared DB.
"""

import asyncio
import logging
from typing import Optional

from .okx_client import OKXClient
from .rotation_strategy import RotationStrategy, RotationConfig, ROT_BOT_ID
from .impulse_strategy import ImpulseStrategy, ImpulseConfig, IMP_BOT_ID

log = logging.getLogger("strategy_manager")


class PerUserClientManager:
    """Lightweight client_manager shim: always returns the user's own OKXClient."""

    def __init__(self, client: Optional[OKXClient]):
        self._client = client

    def get_client(self) -> Optional[OKXClient]:
        return self._client

    def set_client(self, client: Optional[OKXClient]):
        self._client = client


class UserBots:
    """A single user's running strategy instances + their OKX client holder."""

    def __init__(self, telegram_id: str):
        self.telegram_id = telegram_id
        self.client_holder = PerUserClientManager(None)
        self.rotation: Optional[RotationStrategy] = None
        self.impulse: Optional[ImpulseStrategy] = None
        self.validation: Optional[RotationStrategy] = None

    @property
    def rot_bot_id(self) -> str:
        return f"{ROT_BOT_ID}:{self.telegram_id}"

    @property
    def imp_bot_id(self) -> str:
        return f"{IMP_BOT_ID}:{self.telegram_id}"


class StrategyManager:
    def __init__(self, db=None, notifier=None):
        self.db = db
        self.notifier = notifier
        self._users: dict[str, UserBots] = {}

    def get_or_create(self, telegram_id: str) -> UserBots:
        key = str(telegram_id)
        if key not in self._users:
            self._users[key] = UserBots(key)
        return self._users[key]

    def get(self, telegram_id: str) -> Optional[UserBots]:
        return self._users.get(str(telegram_id))

    def set_user_client(self, telegram_id: str, client: Optional[OKXClient]):
        """(Re)bind the user's OKX client. Also rebinds running strategies."""
        ub = self.get_or_create(telegram_id)
        old = ub.client_holder.get_client()
        if old is not None and old is not client:
            try:
                asyncio.get_event_loop()
            except RuntimeError:
                pass
        ub.client_holder.set_client(client)
        if ub.rotation:
            ub.rotation.client_manager = ub.client_holder
        if ub.impulse:
            ub.impulse.client_manager = ub.client_holder
        if ub.validation:
            ub.validation.client_manager = ub.client_holder

    def stop_all(self, telegram_id: str) -> None:
        ub = self.get(telegram_id)
        if not ub:
            return
        for bot in (ub.rotation, ub.impulse, ub.validation):
            if bot and bot._running:
                try:
                    loop = bot._loop
                    if loop and not loop.is_closed():
                        loop.call_soon_threadsafe(loop.stop)
                except Exception:
                    pass
                bot._running = False
        ub.rotation = None
        ub.impulse = None
        ub.validation = None

    def all_bot_ids(self, telegram_id: str) -> list[str]:
        ub = self.get_or_create(telegram_id)
        return [ub.rot_bot_id, ub.imp_bot_id]

    def bot_name_for(self, bot_id: str) -> str:
        """Map a (possibly per-user suffixed) bot_id to a UI bot name."""
        if bot_id in (ROT_BOT_ID, "rotation_strategy", "momentum_strategy"):
            return "Momentum"
        if bot_id.startswith(ROT_BOT_ID + ":"):
            return "Momentum"
        if bot_id == IMP_BOT_ID or bot_id.startswith(IMP_BOT_ID + ":"):
            return "Impulse 1D"
        if bot_id == "validation_strategy":
            return "MACD+Donchian Validation"
        return ""

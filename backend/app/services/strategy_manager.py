
import asyncio
from typing import Optional


class _ClientHolder:
    """Holds a single OKXClient for a user bot."""
    def __init__(self):
        self._client = None

    def init_client(self, client):
        self._client = client

    def get_client(self):
        return self._client


class _UserBots:
    def __init__(self, user_id: str = ""):
        self.user_id = user_id
        self.rotation = None
        self.impulse = None
        self.validation = None
        self.ai = None
        self.client_holder = _ClientHolder()
        self.rot_bot_id = f"rotation_{user_id}"
        self.imp_bot_id = f"impulse_{user_id}"

    def status(self):
        return {}


class StrategyManager:
    def __init__(self, db=None, notifier=None):
        self.db = db
        self.notifier = notifier
        self._users = {}

    def get_or_create(self, user_id):
        if user_id not in self._users:
            self._users[user_id] = _UserBots(user_id)
        return self._users[user_id]

    def set_user_client(self, user_id, client):
        ub = self.get_or_create(user_id)
        ub.client_holder.init_client(client)

    def stop_all(self, user_id=None):
        if user_id:
            ub = self._users.get(user_id)
            if ub:
                self._stop_user_bots(ub)
        else:
            for ub in self._users.values():
                self._stop_user_bots(ub)

    def _stop_user_bots(self, ub):
        for attr in ("rotation", "impulse", "validation", "ai"):
            bot = getattr(ub, attr, None)
            if bot and getattr(bot, "_running", False):
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(bot.stop())
                    else:
                        loop.run_until_complete(bot.stop())
                except Exception:
                    pass
            setattr(ub, attr, None)

    async def hydrate_user_bots(self, db, notifier_fn=None):
        """Auto-restart bots for users with active Pro + OKX keys after deploy."""
        if not db:
            return
        try:
            users = await db.list_users()
        except Exception as e:
            print(f"[SM] hydrate_user_bots: list_users failed: {e}", flush=True)
            return

        from .telegram_bot import _is_active
        started = 0
        for u in (users or []):
            uid = str(u.get("telegram_id") or "")
            if not uid:
                continue
            # Must have OKX keys
            if not (u.get("okx_key_enc") and u.get("okx_secret_enc") and u.get("okx_pass_enc")):
                continue
            # Must have active Pro subscription
            if not _is_active(u):
                continue
            # Skip if already running
            ub = self.get_or_create(uid)
            if ub.rotation and ub.rotation._running:
                continue

            try:
                client = await _user_okx_client_global(uid)
                if not client:
                    continue
                notifier = notifier_fn(uid) if notifier_fn else None
                cfg = _default_rotation_config()
                from .legacy_stubs import RotationStrategy
                bot = RotationStrategy(config=cfg, client_manager=ub.client_holder,
                                       db=db, notifier=notifier)
                bot.BOT_ID = ub.rot_bot_id
                ub.rotation = bot
                await bot.start()
                started += 1
                print(f"[SM] hydrate: started rotation for user {uid}", flush=True)
            except Exception as e:
                print(f"[SM] hydrate: user {uid} rotation start failed: {e}", flush=True)

        if started:
            print(f"[SM] hydrate_user_bots: started {started} user bot(s)", flush=True)


# ── Helpers referenced by hydrate ──

_user_okx_client_global = None
_default_rotation_config_fn = None


def set_hydrate_deps(user_okx_client_fn, default_rotation_config_fn):
    """Called from main.py to inject dependencies for hydrate_user_bots."""
    global _user_okx_client_global, _default_rotation_config_fn
    _user_okx_client_global = user_okx_client_fn
    _default_rotation_config_fn = default_rotation_config_fn


# Alias for backward compatibility
PerUserClientManager = _ClientHolder


def _default_rotation_config():
    if _default_rotation_config_fn:
        return _default_rotation_config_fn()
    from .legacy_stubs import RotationConfig
    return RotationConfig()
    if _default_rotation_config_fn:
        return _default_rotation_config_fn()
    from .legacy_stubs import RotationConfig
    return RotationConfig()

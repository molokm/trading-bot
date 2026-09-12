
"""Minimal multi-tenant stub (retired multi-bot)."""
class _UserBots:
    def __init__(self):
        self.rotation = None
        self.impulse = None
        self.validation = None
        self.ai = None
    def status(self):
        return {}

class StrategyManager:
    def __init__(self, db=None, notifier=None):
        self.db = db
        self.notifier = notifier
        self._users = {}
    def get_or_create(self, user_id):
        if user_id not in self._users:
            self._users[user_id] = _UserBots()
        return self._users[user_id]
    def stop_all(self, user_id=None):
        return None
    def set_user_client(self, user_id, client):
        return None
    def status(self, user_id=None):
        return {}

class PerUserClientManager:
    def __init__(self, *a, **k):
        self._clients = {}
    def get_client(self, *a, **k):
        return None
    def init_client(self, *a, **k):
        return None

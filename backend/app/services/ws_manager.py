import asyncio
import base64
import hmac
import json
import time
from typing import Callable, Optional

import websockets


class WSManager:
    def __init__(self, api_key: str, secret_key: str, passphrase: str, demo: bool = True):
        self.url = (
            "wss://ws.okx.com:8443/ws/v5/private"
            if not demo
            else "wss://wspap.okx.com:8443/ws/v5/private"
        )
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._callbacks: dict[str, list[Callable]] = {}
        self._subscribed: set[tuple[str, str]] = set()
        self._task: Optional[asyncio.Task] = None
        self._pending_pong: Optional[asyncio.Future] = None
        self._last_pong = 0.0

    def on(self, channel: str, callback: Callable):
        self._callbacks.setdefault(channel, []).append(callback)

    def off(self, channel: str, callback: Callable = None):
        if callback:
            self._callbacks[channel] = [c for c in self._callbacks.get(channel, []) if c != callback]
        else:
            self._callbacks.pop(channel, None)

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws:
            await self._ws.close()

    async def subscribe(self, channel: str, inst_id: str = ""):
        self._subscribed.add((channel, inst_id))
        if self._ws:
            await self._send_subscribe(channel, inst_id)

    async def unsubscribe(self, channel: str, inst_id: str = ""):
        self._subscribed.discard((channel, inst_id))
        if self._ws:
            arg = {"channel": channel}
            if inst_id:
                arg["instId"] = inst_id
            await self._safe_send(json.dumps({"op": "unsubscribe", "args": [arg]}))

    async def _run(self):
        retry = 1
        while self._running:
            try:
                self._ws = await websockets.connect(
                    self.url, ping_interval=20, ping_timeout=10, close_timeout=5
                )
                await self._login()
                for ch, inst in self._subscribed:
                    await self._send_subscribe(ch, inst)
                retry = 1
                await self._listen()
            except websockets.ConnectionClosed:
                print(f"[WS] Connection closed, reconnecting in {retry}s", flush=True)
            except OSError as e:
                print(f"[WS] Network error: {e}, reconnecting in {retry}s", flush=True)
            except Exception as e:
                print(f"[WS] Unexpected error: {e}, reconnecting in {retry}s", flush=True)
            if self._running:
                await asyncio.sleep(min(retry, 30))
                retry = min(retry * 2, 30)

    async def _login(self):
        ts = str(int(time.time()))
        msg = f"{ts}GET/users/self/verify"
        sig = base64.b64encode(
            hmac.new(self.secret_key.encode(), msg.encode(), "sha256").digest()
        ).decode()
        login = {
            "op": "login",
            "args": [{
                "apiKey": self.api_key,
                "passphrase": self.passphrase,
                "timestamp": ts,
                "sign": sig,
            }],
        }
        await self._ws.send(json.dumps(login))
        resp = json.loads(await self._ws.recv())
        if resp.get("event") != "login":
            raise ConnectionError(f"WS login failed: {resp}")

    async def _send_subscribe(self, channel: str, inst_id: str = ""):
        arg = {"channel": channel}
        if inst_id:
            arg["instId"] = inst_id
        await self._safe_send(json.dumps({"op": "subscribe", "args": [arg]}))

    async def _safe_send(self, msg: str):
        if self._ws:
            try:
                await self._ws.send(msg)
            except websockets.ConnectionClosed:
                pass

    async def _listen(self):
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if "event" in msg:
                if msg["event"] == "pong":
                    self._last_pong = time.time()
                continue

            if "data" not in msg:
                continue

            channel = msg.get("arg", {}).get("channel", "")
            for cb in self._callbacks.get(channel, []):
                try:
                    await cb(msg["data"])
                except Exception as e:
                    print(f"[WS] Callback error on {channel}: {e}", flush=True)

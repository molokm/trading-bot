import hmac
import base64
import json
import time
import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime

import httpx
import websockets


class OKXClient:
    def __init__(self, api_key: str = "", secret_key: str = "", passphrase: str = "",
                 demo: bool = True):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.demo = demo
        self.base_url = "https://www.okx.com"
        self.ws_url = "wss://ws.okx.com:8443/ws/v5/public"
        self.ws_private_url = "wss://ws.okx.com:8443/ws/v5/private"
        self._ws = None
        self._ws_private = None
        self._ws_callbacks: Dict[str, list] = {}
        self._connected = False
        self._client = httpx.AsyncClient(timeout=30.0)

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        message = f"{timestamp}{method}{path}{body}"
        mac = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            digestmod="sha256"
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        now = datetime.utcnow()
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json"
        }
        if self.demo:
            headers["x-simulated-trading"] = "1"
        return headers

    async def _request(self, method: str, path: str, params: dict = None,
                       body: dict = None) -> dict:
        url = f"{self.base_url}{path}"
        qs = ""
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            if qs:
                url += f"?{qs}"

        body_str = json.dumps(body) if body else ""
        sign_path = f"{path}?{qs}" if qs and method == "GET" else path
        headers = self._headers(method, sign_path, body_str)

        try:
            if method == "GET":
                resp = await self._client.get(url, headers=headers)
            else:
                resp = await self._client.post(url, headers=headers, content=body_str)
            data = resp.json()
            if data.get("code") != "0":
                return {"error": True, "message": data.get("msg", "Unknown error")}
            return {"error": False, "data": data.get("data", [])}
        except Exception as e:
            return {"error": True, "message": str(e)}

    async def get_balance(self) -> dict:
        return await self._request("GET", "/api/v5/account/balance")

    async def get_positions(self, inst_type: str = "SWAP") -> dict:
        return await self._request("GET", "/api/v5/account/positions",
                                    params={"instType": inst_type})

    async def get_ticker(self, inst_id: str) -> dict:
        return await self._request("GET", "/api/v5/market/ticker",
                                    params={"instId": inst_id})

    async def get_candles(self, inst_id: str, bar: str = "1H",
                           after: str = None, before: str = None,
                           limit: int = 300) -> dict:
        params = {"instId": inst_id, "bar": bar, "limit": limit}
        if after:
            params["after"] = after
        if before:
            params["before"] = before
        return await self._request("GET", "/api/v5/market/candles", params=params)

    async def get_instruments(self, inst_type: str = "SPOT") -> dict:
        return await self._request("GET", "/api/v5/public/instruments",
                                    params={"instType": inst_type})

    async def place_order(self, inst_id: str, side: str, ord_type: str,
                           sz: str, px: str = None, td_mode: str = "cash",
                           pos_side: str = None, reduce_only: bool = False,
                           tgt_ccy: str = None) -> dict:
        body = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": ord_type,
            "sz": sz,
        }
        if px:
            body["px"] = px
        if pos_side:
            body["posSide"] = pos_side
        if reduce_only:
            body["reduceOnly"] = True
        if tgt_ccy:
            body["tgtCcy"] = tgt_ccy
        return await self._request("POST", "/api/v5/trade/order", body=body)

    async def cancel_order(self, inst_id: str, ord_id: str) -> dict:
        return await self._request("POST", "/api/v5/cancel-order",
                                    body={"instId": inst_id, "ordId": ord_id})

    async def get_orders(self, inst_type: str = "SWAP", state: str = None,
                          limit: int = 20) -> dict:
        params = {"instType": inst_type, "limit": limit}
        if state:
            params["state"] = state
        return await self._request("GET", "/api/v5/trade/orders", params=params)

    async def get_fills(self, inst_id: str = None, limit: int = 20) -> dict:
        params = {"limit": limit}
        if inst_id:
            params["instId"] = inst_id
        return await self._request("GET", "/api/v5/trade/fills", params=params)

    async def close_position(self, inst_id: str, mgn_mode: str = "cross",
                              pos_side: str = None, auto_cxl: bool = True) -> dict:
        body = {"instId": inst_id, "mgnMode": mgn_mode}
        if pos_side:
            body["posSide"] = pos_side
        if auto_cxl:
            body["autoCxl"] = "true"
        return await self._request("POST", "/api/v5/trade/close-position", body=body)

    async def get_bills(self, inst_type: str = "SWAP", limit: int = 100, **kwargs) -> dict:
        params = {"instType": inst_type, "limit": limit, **kwargs}
        return await self._request("GET", "/api/v5/account/bills", params=params)

    async def close(self):
        await self._client.aclose()
        if self._ws:
            await self._ws.close()
        if self._ws_private:
            await self._ws_private.close()

    @property
    def is_connected(self) -> bool:
        return self._connected

    def has_credentials(self) -> bool:
        return bool(self.api_key and self.secret_key and self.passphrase)

    async def subscribe_ticker(self, inst_id: str, callback):
        if not self._ws:
            self._ws = await websockets.connect(
                "wss://ws.okx.com:8443/ws/v5/public" if not self.demo
                else "wss://wspap.okx.com:8443/ws/v5/public"
            )
            sub = {
                "op": "subscribe",
                "args": [{"channel": "tickers", "instId": inst_id}]
            }
            await self._ws.send(json.dumps(sub))
            asyncio.create_task(self._ws_listener(self._ws, "tickers", callback))

    async def _ws_listener(self, ws, channel: str, callback):
        async for message in ws:
            try:
                data = json.loads(message)
                if "data" in data:
                    await callback(channel, data["data"])
            except json.JSONDecodeError:
                pass


class OKXClientManager:
    _instance: Optional["OKXClientManager"] = None

    def __init__(self):
        self._client: Optional[OKXClient] = None
        self._lock = asyncio.Lock()

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def get_client(self) -> Optional[OKXClient]:
        return self._client

    async def init_client(self, api_key: str, secret_key: str,
                          passphrase: str, demo: bool = True) -> OKXClient:
        async with self._lock:
            if self._client:
                await self._client.close()
            self._client = OKXClient(api_key, secret_key, passphrase, demo)
            self._client._connected = True
            return self._client

    async def test_connection(self, api_key: str, secret_key: str,
                               passphrase: str, demo: bool = True) -> dict:
        client = OKXClient(api_key, secret_key, passphrase, demo)
        try:
            result = await client.get_balance()
            await client.close()
            return result
        except Exception as e:
            await client.close()
            return {"error": True, "message": str(e)}

    def is_ready(self) -> bool:
        return self._client is not None and self._client.has_credentials()

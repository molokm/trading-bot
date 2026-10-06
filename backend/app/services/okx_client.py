import hmac
import base64
import json
import time
import asyncio
import urllib.parse
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum

import httpx
import websockets
from app.services.risk_guard import assert_can_open


class CircuitState(Enum):
    CLOSED = "closed"      # Нормальная работа
    OPEN = "open"          # Короткий замыкание — быстрые ошибки
    HALF_OPEN = "half_open"  # Тестовый запрос после таймаута


class CircuitBreaker:
    """
    Circuit Breaker для защиты от каскадных ошибок при длительном дауне биржи.

    Состояния:
    - CLOSED: нормальная работа, считаем ошибки
    - OPEN: превышен порог ошибок — быстрые ошибки без запросов к бирже
    - HALF_OPEN: после timeout — тестовый запрос для проверки восстановления

    Конфиг:
    - failure_threshold: кол-во ошибок для перехода в OPEN (default: 5)
    - success_threshold: кол-во успехов в HALF_OPEN для CLOSED (default: 2)
    - timeout: секунд в OPEN до перехода в HALF_OPEN (default: 30s)
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        success_threshold: int = 2,
        timeout: float = 30.0,
        excluded_codes: set = None,
    ):
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.timeout = timeout
        self.excluded_codes = excluded_codes or set()

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = asyncio.Lock()

        # Stats
        self._total_calls = 0
        self._total_failures = 0
        self._total_rejected = 0

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def stats(self) -> dict:
        return {
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "total_calls": self._total_calls,
            "total_failures": self._total_failures,
            "total_rejected": self._total_rejected,
            "last_failure_time": self._last_failure_time,
        }

    async def call(self, func, *args, **kwargs):
        """Выполняет функцию с защитой circuit breaker."""
        async with self._lock:
            self._total_calls += 1

            # Проверяем переход OPEN -> HALF_OPEN
            if self._state == CircuitState.OPEN:
                if self._last_failure_time and (time.time() - self._last_failure_time) >= self.timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._success_count = 0
                    print(f"[CircuitBreaker] OPEN -> HALF_OPEN (timeout {self.timeout}s elapsed)", flush=True)
                else:
                    self._total_rejected += 1
                    raise RuntimeError(f"Circuit breaker OPEN (rejected)")

        try:
            result = await func(*args, **kwargs)
        except Exception as e:
            await self._on_failure()
            raise

        # Проверяем результат на ошибку бизнес-уровня
        if isinstance(result, dict) and result.get("error"):
            error_msg = str(result.get("message", ""))
            # Не считаем ошибки бизнес-уровня (insufficient margin, etc) как failure
            if not self._is_business_error(result):
                await self._on_failure()
            return result

        await self._on_success()
        return result

    def _is_business_error(self, result: dict) -> bool:
        """Ошибки бизнес-уровня (недостаток маржи, неверные параметры) — не считаем failure."""
        msg = str(result.get("message", "")).lower()
        business_codes = {"58001", "58002", "58003", "58004", "58005",  # margin/position
                          "30001", "30002", "30003",  # param errors
                          "58101", "58102",  # leverage
                          "51008"}  # margin quota
        for code in self.excluded_codes:
            if code.lower() in str(result).lower():
                return True
        for code in business_codes:
            if code in str(result).lower():
                return True
        return False

    async def _on_success(self):
        async with self._lock:
            self._failure_count = 0
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._success_count = 0
                    print(f"[CircuitBreaker] HALF_OPEN -> CLOSED (success threshold reached)", flush=True)

    async def _on_failure(self):
        async with self._lock:
            self._failure_count += 1
            self._total_failures += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                # Любая ошибка в HALF_OPEN -> обратно в OPEN
                self._state = CircuitState.OPEN
                self._success_count = 0
                print(f"[CircuitBreaker] HALF_OPEN -> OPEN (failure in half-open)", flush=True)
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    print(f"[CircuitBreaker] CLOSED -> OPEN (threshold {self.failure_threshold} reached)", flush=True)

    def reset(self):
        """Ручной сброс в CLOSED (для админки/тестов)."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = None

    def force_open(self):
        """Принудительно открыть (для тестов/админки)."""
        self._state = CircuitState.OPEN
        self._last_failure_time = time.time()


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
        # v1.17 perf: lazily created, cached PER EVENT LOOP (not per-request).
        # Previously _request() created+tore down a fresh httpx.AsyncClient on
        # every single call (even every retry attempt within one call) — each
        # one pays a full TCP+TLS handshake (~50-300ms) that connection reuse
        # would otherwise avoid. The original reason for not sharing a single
        # client was event-loop binding errors across strategy threads; caching
        # per-loop (recreating only if the running loop actually changed) keeps
        # that safety while still reusing connections in the common case.
        self._client: Optional[httpx.AsyncClient] = None
        self._client_loop = None

        # Circuit Breaker для защиты от каскадных ошибок
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=5,      # 5 ошибок -> OPEN
            success_threshold=2,      # 2 успеха в half-open -> CLOSED
            timeout=30.0,             # 30с в OPEN -> HALF_OPEN
            excluded_codes={"58001", "58002", "58003", "58004", "58005",
                           "30001", "30002", "30003",
                           "58101", "58102", "51008", "58008", "58009"},
        )

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

    async def _get_client(self) -> httpx.AsyncClient:
        """Return an AsyncClient bound to the *currently running* event loop,
        reused across calls. Recreated only if the loop changed (e.g. a
        different strategy thread picked up this same OKXClient instance) or
        the cached client was already closed — otherwise every call reuses
        the same connection pool instead of paying a fresh TCP+TLS handshake.
        """
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop or self._client.is_closed:
            if self._client is not None and not self._client.is_closed:
                try:
                    await self._client.aclose()
                except Exception:
                    pass
            self._client = httpx.AsyncClient(timeout=30.0)
            self._client_loop = loop
        return self._client

    async def _request(self, method: str, path: str, params: dict = None,
                       body: dict = None) -> dict:
        """HTTP to OKX via per-loop client reuse + circuit breaker + resign-on-retry."""
        async def _do_request() -> dict:
            url = f"{self.base_url}{path}"
            qs = ""
            if params:
                filtered = {k: v for k, v in params.items() if v is not None}
                if filtered:
                    qs = urllib.parse.urlencode(filtered)
                    url += f"?{qs}"
            body_str = json.dumps(body) if body else ""
            sign_path = f"{path}?{qs}" if qs and method == "GET" else path
            RATE_LIMIT_CODES = {"50011", "50013", "50010", "50019"}
            last_err = None
            for attempt in range(4):
                try:
                    # Resign each attempt — cumulative backoff can stale timestamp
                    headers = self._headers(method, sign_path, body_str)
                    _client = await self._get_client()
                    if method == "GET":
                        resp = await _client.get(url, headers=headers)
                    else:
                        resp = await _client.post(url, headers=headers, content=body_str)
                    data = resp.json()

                    if resp.status_code == 429:
                        wait = 1.0 + attempt * 2.0
                        last_err = f"HTTP 429 rate limited (attempt {attempt + 1})"
                        await asyncio.sleep(wait)
                        continue

                    if data.get("code") != "0":
                        code = str(data.get("code", ""))
                        if code in RATE_LIMIT_CODES and attempt < 3:
                            wait = 1.0 + attempt * 2.0
                            last_err = f"OKX {code} throttled (attempt {attempt + 1})"
                            await asyncio.sleep(wait)
                            continue
                        detail = data.get("msg", "Unknown error")
                        sdata = data.get("data") or []
                        if sdata:
                            scode = sdata[0].get("sCode", "")
                            smsg = sdata[0].get("sMsg", "")
                            if scode or smsg:
                                detail = f"{detail} [{scode}: {smsg}]"
                            return {"error": True, "message": detail, "data": sdata}
                        return {"error": True, "message": detail}

                    return {"error": False, "data": data.get("data", [])}
                except (httpx.HTTPStatusError, httpx.ConnectError, httpx.ConnectTimeout,
                        httpx.ReadTimeout, httpx.TransportError) as e:
                    last_err = str(e)
                    await asyncio.sleep(1.0 + attempt)
                except Exception as e:
                    return {"error": True, "message": str(e)}
            return {"error": True, "message": f"request failed after retries: {last_err}"}

        return await self._circuit_breaker.call(_do_request)

    async def get_balance(self) -> dict:
        return await self._request("GET", "/api/v5/account/balance")

    async def get_positions(self, inst_type: str = "SWAP", inst_id: str = None) -> dict:
        params = {"instType": inst_type}
        if inst_id:
            params["instId"] = inst_id
        return await self._request("GET", "/api/v5/account/positions", params=params)

    async def get_ticker(self, inst_id: str) -> dict:
        return await self._request("GET", "/api/v5/market/ticker",
                                    params={"instId": inst_id})

    async def get_funding_rate(self, inst_id: str) -> dict:
        """Public funding rate for SWAP (fundingRate, nextFundingRate, fundingTime)."""
        return await self._request(
            "GET", "/api/v5/public/funding-rate",
            params={"instId": inst_id},
        )

    async def get_books(self, inst_id: str, sz: int = 20) -> dict:
        """Order book depth. sz = levels per side (1..400)."""
        return await self._request(
            "GET", "/api/v5/market/books",
            params={"instId": inst_id, "sz": str(max(1, min(int(sz), 400)))},
        )

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
                           tgt_ccy: str = None, cl_ord_id: str = None,
                           is_close: bool = False) -> dict:
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
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        # Block new risk-increasing orders when kill switch / daily loss limits hit.
        # reduce_only / explicit closes always pass. is_close marks a position
        # reduction in hedge mode (where OKX forbids the reduceOnly flag).
        symbol = inst_id.split("-")[0] if inst_id else None
        try:
            assert_can_open(is_reduce_only=bool(reduce_only or is_close), symbol=symbol)
        except RuntimeError as e:
            return {"error": True, "message": str(e), "code": "risk_guard"}
        return await self._request("POST", "/api/v5/trade/order", body=body)

    async def cancel_order(self, inst_id: str, ord_id: str) -> dict:
        return await self._request("POST", "/api/v5/cancel-order",
                                    body={"instId": inst_id, "ordId": ord_id})

    async def get_order(self, inst_id: str, ord_id: str = None,
                        cl_ord_id: str = None) -> dict:
        """GET /api/v5/trade/order — avgPx, fee, state for a single order."""
        params = {"instId": inst_id}
        if ord_id:
            params["ordId"] = ord_id
        if cl_ord_id:
            params["clOrdId"] = cl_ord_id
        return await self._request("GET", "/api/v5/trade/order", params=params)

    async def get_orders(self, inst_type: str = "SWAP", state: str = None,
                          limit: int = 20) -> dict:
        params = {"instType": inst_type, "limit": limit}
        if state:
            params["state"] = state
        return await self._request("GET", "/api/v5/trade/orders", params=params)

    async def get_fills(self, inst_id: str = None, limit: int = 20, **kwargs) -> dict:
        params = {"limit": limit, **kwargs}
        if inst_id:
            params["instId"] = inst_id
        return await self._request("GET", "/api/v5/trade/fills", params=params)

    async def get_fills_history(self, inst_type: str = "SWAP", limit: int = 100, **kwargs) -> dict:
        """OKX /api/v5/trade/fills-history — returns past 3 months of fills."""
        params = {"instType": inst_type, "limit": limit, **kwargs}
        return await self._request("GET", "/api/v5/trade/fills-history", params=params)

    async def set_leverage(self, inst_id: str, leverage: float, mgn_mode: str = "cross", pos_side: str = "net") -> dict:
        """Set leverage for an instrument. OKX /api/v5/account/set-leverage."""
        body = {
            "instId": inst_id,
            "lever": str(leverage),
            "mgnMode": mgn_mode,
        }
        if pos_side:
            body["posSide"] = pos_side
        return await self._request("POST", "/api/v5/account/set-leverage", body=body)

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

    async def get_bills_archive(self, inst_type: str = "SWAP", limit: int = 100, **kwargs) -> dict:
        """Account bills older than 7 days (up to 3 months)."""
        params = {"instType": inst_type, "limit": limit, **kwargs}
        return await self._request("GET", "/api/v5/account/bills-archive", params=params)

    async def get_algo_orders(self, ord_type: str = "conditional", inst_type: str = "SWAP",
                              state: str = "live", limit: int = 50, **kwargs) -> dict:
        """Get algo orders (TP/SL/conditional). OKX /api/v5/trade/orders-algo-pending."""
        params = {"ordType": ord_type, "instType": inst_type, "state": state, "limit": limit, **kwargs}
        return await self._request("GET", "/api/v5/trade/orders-algo-pending", params=params)

    async def place_algo_order(self, inst_id: str, side: str, sz: str,
                               td_mode: str = "cross", pos_side: str = None,
                               reduce_only: bool = False,
                               sl_trigger_px: str = None, sl_ord_px: str = "-1",
                               tp_trigger_px: str = None, tp_ord_px: str = "-1",
                               cxl_on_close_pos: bool = False,
                               cl_ord_id: str = None) -> dict:
        """Place a conditional (TP/SL) algo order. OKX /api/v5/trade/order-algo."""
        body = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": "conditional",
            "sz": sz,
        }
        if pos_side:
            body["posSide"] = pos_side
        if reduce_only:
            body["reduceOnly"] = "true"
        if cxl_on_close_pos:
            body["cxlOnClosePos"] = "true"
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        if sl_trigger_px:
            body["slTriggerPx"] = sl_trigger_px
            body["slOrdPx"] = sl_ord_px or "-1"
        if tp_trigger_px:
            body["tpTriggerPx"] = tp_trigger_px
            body["tpOrdPx"] = tp_ord_px or "-1"
        return await self._request("POST", "/api/v5/trade/order-algo", body=body)

    async def amend_algo_order(self, inst_id: str, algo_id: str,
                               ord_type: str = "conditional",
                               sl_trigger_px: str = None, sl_ord_px: str = "-1",
                               tp_trigger_px: str = None, tp_ord_px: str = "-1") -> dict:
        """Amend a conditional (TP/SL) algo order. OKX /api/v5/trade/amend-algos."""
        body = {
            "instId": inst_id,
            "algoId": algo_id,
            "ordType": ord_type,
        }
        if sl_trigger_px:
            body["slTriggerPx"] = sl_trigger_px
            body["slOrdPx"] = sl_ord_px or "-1"
        if tp_trigger_px:
            body["tpTriggerPx"] = tp_trigger_px
            body["tpOrdPx"] = tp_ord_px or "-1"
        return await self._request("POST", "/api/v5/trade/amend-algos", body=body)

    async def cancel_algo_order(self, inst_id: str, algo_id: str,
                                ord_type: str = "conditional") -> dict:
        """Cancel a single algo order. OKX /api/v5/trade/cancel-algos."""
        body = [{
            "algoId": algo_id,
            "instId": inst_id,
            "ordType": ord_type,
        }]
        return await self._request("POST", "/api/v5/trade/cancel-algos", body=body)

    async def close(self):
        if self._ws:
            await self._ws.close()
        if self._ws_private:
            await self._ws_private.close()
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception:
                pass

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

    @classmethod
    def new_instance(cls) -> "OKXClientManager":
        """Separate manager (e.g. permanent showcase DEMO) — not the singleton."""
        return cls()

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

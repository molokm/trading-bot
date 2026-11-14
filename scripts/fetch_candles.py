"""Fetch 1H OKX candles for the AI Discretionary universe and cache them.

Usage:  python scripts/fetch_candles.py [bars] [out_dir]
Default: 1000 bars per symbol into /tmp/candles_1h
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

SYMBOLS = [
    "BTC", "ETH", "SOL", "XRP", "DOGE", "BCH", "OKB",
    "LINK", "AVAX", "ADA", "LTC", "DOT", "UNI", "NEAR",
]
BASE = "https://www.okx.com"
PAGE = 300


def get(inst_id: str, after: str = "") -> list:
    # `after` pages BACKWARDS in time (records older than the given ts);
    # `before` pages forwards and would just re-read the newest window.
    q = {"instId": inst_id, "bar": "1H", "limit": str(PAGE)}
    if after:
        q["after"] = after
    url = BASE + "/api/v5/market/candles?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode())
            if str(d.get("code")) != "0":
                raise RuntimeError(d.get("msg"))
            return d.get("data") or []
        except Exception as e:
            if attempt == 3:
                raise
            time.sleep(0.6 * (attempt + 1))
    return []


def fetch(inst_id: str, bars: int) -> list:
    out: list = []
    after = ""
    while len(out) < bars:
        rows = get(inst_id, after)
        if not rows:
            break
        out.extend(rows)
        after = rows[-1][0]
        if len(rows) < PAGE:
            break
        time.sleep(0.25)
    # newest-first -> keep the newest `bars`, dedupe
    seen = set()
    uniq = []
    for r in out:
        if r[0] in seen:
            continue
        seen.add(r[0])
        uniq.append(r)
    return uniq[:bars]


def main() -> None:
    bars = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "/tmp/candles_1h"
    os.makedirs(out_dir, exist_ok=True)
    for s in SYMBOLS:
        path = os.path.join(out_dir, f"{s}.json")
        if os.path.exists(path) and os.path.getsize(path) > 1000:
            print(f"skip {s} (cached)", flush=True)
            continue
        try:
            rows = fetch(f"{s}-USDT-SWAP", bars)
        except Exception as e:
            print(f"FAIL {s}: {e}", flush=True)
            continue
        with open(path, "w") as f:
            json.dump(rows, f)
        print(f"{s}: {len(rows)} bars", flush=True)


if __name__ == "__main__":
    main()

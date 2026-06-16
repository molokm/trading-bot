import asyncio, httpx, json
from datetime import datetime
from pathlib import Path


async def main():
    start_sec = int(datetime(2025, 6, 14).timestamp())
    end_sec = int(datetime(2026, 6, 14).timestamp())
    per_sec = 86400
    total_days = (end_sec - start_sec) // per_sec
    print(f"Total days: {total_days}")

    collected = {}

    async with httpx.AsyncClient(timeout=15) as client:
        for day in range(total_days):
            s = start_sec + day * per_sec
            e = min(s + per_sec, end_sec)
            try:
                r = await client.get(
                    "https://api.kucoin.com/api/v1/market/candles",
                    params={"symbol": "BTC-USDT", "type": "1min",
                            "startAt": str(s), "endAt": str(e)},
                )
                d = r.json()
                if d.get("code") == "200000":
                    for k in d.get("data", []):
                        ts = int(k[0]) * 1000
                        collected[ts] = [str(ts)] + k[1:]
                if day % 30 == 0:
                    cnt = len(collected)
                    print(f"  Day {day}/{total_days}: {datetime.fromtimestamp(s).date()}, unique so far: {cnt}")
            except Exception as ex:
                print(f"  Day {day} error: {ex}")
            await asyncio.sleep(0.35)

    all_candles = [collected[k] for k in sorted(collected.keys())]
    print(f"\nTotal unique: {len(all_candles)} candles")

    if all_candles:
        first = int(all_candles[0][0])
        last = int(all_candles[-1][0])
        print(f"Range: {datetime.fromtimestamp(first / 1000).date()} to {datetime.fromtimestamp(last / 1000).date()}")

        cache_dir = Path("backtests_data/candles")
        cache_dir.mkdir(parents=True, exist_ok=True)
        p = cache_dir / "BTC_USDT_1m.json"
        p.write_text(json.dumps({
            "symbol": "BTC-USDT", "timeframe": "1m",
            "start_ms": start_sec * 1000, "end_ms": end_sec * 1000,
            "count": len(all_candles), "candles": all_candles,
        }, ensure_ascii=False, default=str))
        print(f"Saved to {p}")


if __name__ == "__main__":
    asyncio.run(main())

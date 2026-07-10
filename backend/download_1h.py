"""Download 1H candles from Binance - fast version."""
import httpx, pandas as pd, time, sys
from pathlib import Path

CANDLES_DIR = Path(__file__).parent / "data" / "candles"
CANDLES_DIR.mkdir(parents=True, exist_ok=True)

symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
start_ms = int(sys.argv[2]) if len(sys.argv) > 2 else 1704067200000  # 2024-01-01 default

all_rows = []
end_ms = int(time.time() * 1000)
retries = 0

print(f"Downloading {symbol} 1H from {pd.to_datetime(start_ms, unit='ms')}...")

while retries < 5:
    params = {"symbol": symbol, "interval": "1h", "limit": 1000, "endTime": end_ms}
    try:
        resp = httpx.get("https://api.binance.com/api/v3/klines", params=params, timeout=15)
        data = resp.json()
        retries = 0
    except Exception as e:
        retries += 1
        print(f"  Retry {retries}: {e}")
        time.sleep(2)
        continue

    if not data or not isinstance(data, list) or len(data) == 0:
        break

    added = 0
    for c in data:
        ts_ms = int(c[0])
        if ts_ms < start_ms:
            break
        all_rows.append({
            "ts": pd.to_datetime(ts_ms, unit="ms"),
            "Open": float(c[1]),
            "High": float(c[2]),
            "Low": float(c[3]),
            "Close": float(c[4]),
            "Volume": float(c[5]),
        })
        added += 1

    if added == 0 or len(data) < 1000:
        break

    end_ms = int(data[-1][0]) - 1
    time.sleep(0.12)

df = pd.DataFrame(all_rows)
df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
path = CANDLES_DIR / f"{symbol}_1H.csv"
df.to_csv(path, index=False)
print(f"Saved {len(df)} candles to {path}")
if len(df) > 0:
    print(f"Range: {df['ts'].iloc[0]} to {df['ts'].iloc[-1]}")

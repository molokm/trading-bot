import json
import os
from pathlib import Path
from typing import Optional, List
from datetime import datetime

STRATEGIES_DIR = Path(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))) / "strategies"
BACKTESTS_DIR = Path(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))) / "backtests_data"


def ensure_dirs():
    STRATEGIES_DIR.mkdir(exist_ok=True)
    BACKTESTS_DIR.mkdir(exist_ok=True)


def list_strategies() -> List[dict]:
    ensure_dirs()
    strategies = []
    for f in STRATEGIES_DIR.iterdir():
        if f.suffix in (".py", ".json"):
            meta = parse_strategy_file(f)
            if meta:
                strategies.append(meta)
    return strategies


def parse_strategy_file(path: Path) -> Optional[dict]:
    try:
        content = path.read_text(encoding="utf-8")
        meta = {
            "id": path.stem,
            "filename": path.name,
            "uploaded_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
        }

        if path.suffix == ".json":
            data = json.loads(content)
            meta["name"] = data.get("name", path.stem)
            meta["description"] = data.get("description", "")
            meta["timeframe"] = data.get("timeframe", "1H")
            meta["symbol"] = data.get("symbol", "BTC-USDT")
            meta["params"] = data.get("params", {})
            meta["code"] = data.get("code", "")
        else:
            meta["name"] = path.stem
            meta["description"] = ""
            meta["timeframe"] = "1H"
            meta["symbol"] = "BTC-USDT"
            meta["params"] = {}
            meta["code"] = content

            for line in content.split("\n")[:30]:
                line = line.strip()
                if line.startswith("# @"):
                    key_val = line[3:].split(":", 1)
                    if len(key_val) == 2:
                        key, val = key_val[0].strip(), key_val[1].strip()
                        if key == "name":
                            meta["name"] = val
                        elif key == "description":
                            meta["description"] = val
                        elif key == "timeframe":
                            meta["timeframe"] = val
                        elif key == "symbol":
                            meta["symbol"] = val
                        elif key == "params":
                            try:
                                meta["params"] = json.loads(val)
                            except (json.JSONDecodeError, TypeError):
                                pass

        return meta
    except Exception:
        return None


def get_strategy_code(strategy_id: str) -> Optional[str]:
    ensure_dirs()
    for ext in (".py", ".json"):
        path = STRATEGIES_DIR / f"{strategy_id}{ext}"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data.get("code", "")
            except (json.JSONDecodeError, Exception):
                return path.read_text(encoding="utf-8")
    return None


def save_strategy(filename: str, content: str) -> bool:
    ensure_dirs()
    try:
        path = STRATEGIES_DIR / filename
        path.write_text(content, encoding="utf-8")
        return True
    except Exception:
        return False


def delete_strategy(strategy_id: str) -> bool:
    for ext in (".py", ".json"):
        path = STRATEGIES_DIR / f"{strategy_id}{ext}"
        if path.exists():
            path.unlink()
            return True
    return False


def save_backtest_result(strategy_id: str, result: dict) -> str:
    ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{strategy_id}_{ts}.json"
    path = BACKTESTS_DIR / filename
    path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return filename


def list_backtest_results() -> List[dict]:
    ensure_dirs()
    results = []
    for f in sorted(BACKTESTS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.suffix == ".json":
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                results.append({
                    "id": f.stem,
                    "strategy_name": data.get("strategy_name", "Unknown"),
                    "symbol": data.get("symbol", ""),
                    "total_return_pct": data.get("total_return_pct", 0),
                    "sharpe_ratio": data.get("sharpe_ratio", 0),
                    "max_drawdown": data.get("max_drawdown", 0),
                    "total_trades": data.get("total_trades", 0),
                    "file": f.name
                })
            except Exception:
                pass
    return results

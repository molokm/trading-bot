"""AnalysisLogger — writes every bot decision to a JSONL file for later analysis.

One JSON object per line (JSONL). Thread-safe: both bots (rotation, impulse) run
in their own threads and may log to the same file concurrently.

Default path: <backend>/logs/analysis.jsonl
Override with env ANALYSIS_LOG_PATH (absolute or relative to CWD).

Rotation (size-based): when the file exceeds max_bytes it is renamed to
analysis.jsonl.1, analysis.jsonl.2, ... and a fresh file is started.
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_PATH = os.getenv("ANALYSIS_LOG_PATH",
                         str(_BACKEND_DIR / "logs" / "analysis.jsonl"))
DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MB per file


class AnalysisLogger:
    """JSONL append-only logger with lock-protected writes and size rotation."""

    def __init__(self, path: str = None, max_bytes: int = DEFAULT_MAX_BYTES):
        self.path = path or DEFAULT_PATH
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._fh = None
        self._open()

    def _open(self):
        try:
            p = Path(self.path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(p, "a", encoding="utf-8")
        except Exception as e:
            print(f"[analysis] cannot open log file {self.path}: {e}", flush=True)
            self._fh = None

    def log(self, bot: str, event: str, **data):
        """Append one event line. Never raises (best-effort)."""
        if not self._fh:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "bot": bot,
            "event": event,
        }
        record.update(data)
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
            with self._lock:
                self._fh.write(line + "\n")
                self._fh.flush()
                self._rotate_if_needed()
        except Exception as e:
            print(f"[analysis] write error: {e}", flush=True)

    def _rotate_if_needed(self):
        if not self._fh or self.max_bytes <= 0:
            return
        try:
            if self._fh.tell() < self.max_bytes:
                return
            self._fh.close()
            n = 1
            while os.path.exists(f"{self.path}.{n}"):
                n += 1
            os.rename(self.path, f"{self.path}.{n}")
            self._open()
        except Exception as e:
            print(f"[analysis] rotation error: {e}", flush=True)

    def close(self):
        with self._lock:
            if self._fh:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None


# Shared instance used by default when bots are constructed without an explicit
# logger — guarantees a single file handle + single lock across both bots.
_default_logger = None


def get_logger(path: str = None, max_bytes: int = DEFAULT_MAX_BYTES) -> AnalysisLogger:
    global _default_logger
    if _default_logger is None or (path and path != _default_logger.path):
        _default_logger = AnalysisLogger(path=path, max_bytes=max_bytes)
    return _default_logger

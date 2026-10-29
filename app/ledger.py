import json
import os
import threading

from .config import DATA_DIR, LEDGER_PATH
from .models import LedgerEntry

_lock = threading.Lock()


def load_all() -> list[LedgerEntry]:
    if not LEDGER_PATH.exists():
        return []
    with open(LEDGER_PATH, encoding="utf-8") as f:
        return [LedgerEntry.model_validate(entry) for entry in json.load(f)]


def append(entry: LedgerEntry) -> None:
    with _lock:
        entries = [e.model_dump() for e in load_all()]
        entries.append(entry.model_dump())
        _write(entries)


def delete(uuid: str) -> bool:
    """Remove the entry; False when there is no entry with that uuid."""
    with _lock:
        entries = [e.model_dump() for e in load_all()]
        remaining = [e for e in entries if e["uuid"] != uuid]
        if len(remaining) == len(entries):
            return False
        _write(remaining)
        return True


def _write(entries: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    tmp_path = LEDGER_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, LEDGER_PATH)

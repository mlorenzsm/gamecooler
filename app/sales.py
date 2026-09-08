import json
import os
import threading

from .config import DATA_DIR, SALES_PATH
from .models import Sale

_lock = threading.Lock()


def load_all() -> list[Sale]:
    if not SALES_PATH.exists():
        return []
    with open(SALES_PATH, encoding="utf-8") as f:
        return [Sale.model_validate(entry) for entry in json.load(f)]


def next_number() -> int:
    sales = load_all()
    return max((s.number for s in sales), default=0) + 1


def append(sale: Sale) -> None:
    with _lock:
        entries = [s.model_dump() for s in load_all()]
        entries.append(sale.model_dump())
        _write(entries)


def get(sale_id: str) -> Sale | None:
    for sale in load_all():
        if sale.sale_id == sale_id:
            return sale
    return None


def _write(entries: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    tmp_path = SALES_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, SALES_PATH)

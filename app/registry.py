import json
import os
import threading

from .config import DATA_DIR, REGISTRY_PATH
from .models import PartRecord

_lock = threading.Lock()


def load_all() -> list[PartRecord]:
    if not REGISTRY_PATH.exists():
        return []
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        return [PartRecord.model_validate(entry) for entry in json.load(f)]


def append(record: PartRecord) -> None:
    with _lock:
        entries = [r.model_dump() for r in load_all()]
        entries.append(record.model_dump())
        DATA_DIR.mkdir(exist_ok=True)
        tmp_path = REGISTRY_PATH.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, REGISTRY_PATH)


def get(part_uuid: str) -> PartRecord | None:
    for record in load_all():
        if record.uuid == part_uuid:
            return record
    return None


def recent(n: int = 20) -> list[PartRecord]:
    return list(reversed(load_all()))[:n]

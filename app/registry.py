import json
import os
import threading
from datetime import datetime, timezone

from .config import DATA_DIR, REGISTRY_PATH
from .models import PartRecord

_lock = threading.Lock()


def load_all() -> list[PartRecord]:
    if not REGISTRY_PATH.exists():
        return []
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        return [PartRecord.model_validate(entry) for entry in json.load(f)]


def append(record: PartRecord) -> None:
    append_many([record])


def append_many(records: list[PartRecord]) -> None:
    with _lock:
        entries = [r.model_dump() for r in load_all()]
        entries.extend(r.model_dump() for r in records)
        _write(entries)


def mark_consumed(part_uuid: str) -> tuple[PartRecord, bool] | None:
    """Returns (record, was_already_consumed), or None if unknown."""
    with _lock:
        records = load_all()
        for record in records:
            if record.uuid == part_uuid:
                if record.consumed_at is not None:
                    return record, True
                record.consumed_at = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )
                _write([r.model_dump() for r in records])
                return record, False
        return None


def mark_consumed_many(part_uuids: list[str]) -> int:
    """Marks all given uuids consumed in one write; returns count of newly consumed."""
    wanted = set(part_uuids)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        records = load_all()
        changed = 0
        for record in records:
            if record.uuid in wanted and record.consumed_at is None:
                record.consumed_at = now
                changed += 1
        if changed:
            _write([r.model_dump() for r in records])
        return changed


def inventory() -> list[PartRecord]:
    return [r for r in load_all() if r.consumed_at is None]


def _write(entries: list[dict]) -> None:
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

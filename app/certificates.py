"""Trichinenuntersuchung documents, one per Wildursprungsmarke.

Uploaded on the print forms next to the mark. The file on disk is the source
of truth; Paperless (when configured) gets a copy in the background. Stored
as data/certificates/<mark>.<ext> — a new upload for the same mark replaces
the old file, whatever its type was.
"""

import re
from pathlib import Path

from .config import DATA_DIR

CERT_DIR = DATA_DIR / "certificates"

# Lab reports arrive as a scan or a phone photo; nothing else is expected.
ALLOWED = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".png": "image/png"}
MAX_UPLOAD = 15 * 1024 * 1024


def _safe(mark: str) -> str:
    """The mark as a filename. Marks are short codes (DE-HE-…), but they come
    from a free-text field, so anything path-like is flattened."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", mark.strip()) or "_"


def path(mark: str) -> Path | None:
    """The stored document for a mark, or None."""
    for ext in ALLOWED:
        p = CERT_DIR / f"{_safe(mark)}{ext}"
        if p.exists():
            return p
    return None


def exists(mark: str) -> bool:
    return path(mark) is not None


def save(mark: str, filename: str, content: bytes) -> Path:
    """Store a document for a mark, replacing any earlier one.

    Raises ValueError for a file type that isn't a PDF, JPG or PNG.
    """
    ext = Path(filename or "").suffix.lower()
    if ext == ".jpeg":
        ext = ".jpg"
    if ext not in ALLOWED:
        raise ValueError(ext)
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    old = path(mark)
    target = CERT_DIR / f"{_safe(mark)}{ext}"
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(target)
    if old is not None and old != target:
        old.unlink(missing_ok=True)
    return target

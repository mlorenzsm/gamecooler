"""Upload the public stock page to the web host.

The app is LAN-only; the page a customer scans lives on shared hosting. This
module takes the bundle from `public_site.build()` and puts it there over
**FTPS** — FTP with TLS, which is what the host offers. SFTP would need SSH,
and that isn't part of the hosting plan.

Configuration comes from the environment, never from config.yaml, because the
password must not end up in a file the settings page writes. The service reads
`/etc/gamecooler/publish.env` (see deploy/publish.env.example):

    PUBLISH_HOST=ftp.wildlorenz.de
    PUBLISH_USER=...
    PUBLISH_PASSWORD=...
    PUBLISH_PATH=httpdocs

Missing settings mean "not published" and every call is a no-op, exactly like
Paperless. A failed upload must never fail the stock change that triggered it —
the part is already in the freezer and in the registry either way.
"""

import ftplib
import io
import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# Result of the last attempt, for the settings page. In memory: a restart
# clears it, and the next change publishes again anyway.
_last: dict = {"at": "", "ok": None, "error": ""}

# A short timeout: the upload runs in the background and a hanging FTP server
# must not pin a worker thread for minutes.
TIMEOUT = 20


def settings() -> dict:
    return {
        "host": os.environ.get("PUBLISH_HOST", "").strip(),
        "port": int(os.environ.get("PUBLISH_PORT", "21") or 21),
        "user": os.environ.get("PUBLISH_USER", "").strip(),
        "password": os.environ.get("PUBLISH_PASSWORD", ""),
        "path": os.environ.get("PUBLISH_PATH", "httpdocs").strip().strip("/"),
    }


def enabled() -> bool:
    s = settings()
    return bool(s["host"] and s["user"] and s["password"])


def last_result() -> dict:
    return dict(_last)


def _ensure_dir(ftp: ftplib.FTP_TLS, path: str) -> None:
    """Create `path` relative to the current directory, then come back.

    The caller keeps the connection at the bundle root; walking into a
    subdirectory to create it would otherwise leave every later relative path
    resolved from the wrong place.
    """
    if not path:
        return
    start = ftp.pwd()
    try:
        current = ""
        for part in path.strip("/").split("/"):
            current = f"{current}/{part}" if current else part
            try:
                ftp.cwd(current)
            except ftplib.error_perm:
                ftp.mkd(current)
                ftp.cwd(current)
    finally:
        ftp.cwd(start)


def upload(files: dict[str, bytes]) -> None:
    """Upload the bundle; raises on failure so the caller can record it.

    Files go up as `<name>.upload` and are renamed into place afterwards, so a
    visitor never loads a half-written page. Unchanged files are skipped by
    size — the fonts are 90 KB and shared hosting is not fast.

    Paths are relative to the bundle root: the connection is chdir'd there
    once, and a nested file like fonts/geist.woff2 creates its directory from
    there and comes back, so the next file starts in the right place.
    """
    s = settings()
    with _lock:
        ftp = ftplib.FTP_TLS(timeout=TIMEOUT)
        try:
            ftp.connect(s["host"], s["port"])
            ftp.login(s["user"], s["password"])
            ftp.prot_p()                      # encrypt the data channel too
            if s["path"]:
                _ensure_dir(ftp, s["path"])
                ftp.cwd(s["path"])
            def sizes_in(directory: str) -> dict[str, int]:
                """Name → size for one directory, relative to the bundle root."""
                start = ftp.pwd()
                try:
                    if directory:
                        ftp.cwd(directory)
                    return {name: int(f["size"]) for name, f in ftp.mlsd() if "size" in f}
                except ftplib.error_perm:
                    # MLSd isn't universal; upload everything in that directory
                    # instead of comparing. Correct, just slower.
                    logger.info("publish: MLSd not available for %s", directory or ".")
                    return {}
                finally:
                    ftp.cwd(start)

            uploaded = 0
            for name, data in files.items():
                # Relative to the bundle root we are already in. The directory
                # for a nested file ("fonts/x.woff2") is created from there.
                parent, _, base = name.rpartition("/")
                if parent:
                    _ensure_dir(ftp, parent)
                if sizes_in(parent).get(base) == len(data):
                    continue
                tmp = name + ".upload"
                ftp.storbinary(f"STOR {tmp}", io.BytesIO(data))
                # RNFR/RNTO replaces atomically where the server supports it
                try:
                    ftp.rename(tmp, name)
                except ftplib.error_perm:
                    ftp.delete(name)
                    ftp.rename(tmp, name)
                uploaded += 1
            logger.info("publish: uploaded %d of %d files", uploaded, len(files))
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()


def publish(files: dict[str, bytes]) -> bool:
    """Upload and record the outcome; never raises. Returns True on success."""
    global _last
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not enabled():
        return False
    try:
        upload(files)
        _last = {"at": stamp, "ok": True, "error": ""}
        return True
    except Exception as e:
        _last = {"at": stamp, "ok": False, "error": str(e)}
        logger.warning("publish failed: %s", e)
        return False

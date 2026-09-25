"""Uploads sale invoices to Paperless-ngx.

Configured by two environment variables, set through a systemd
EnvironmentFile (see docs/paperless.md):

    PAPERLESS_URL    e.g. https://paperless.home.arpa
    PAPERLESS_TOKEN  API token of a Paperless user

Without both, the feature is off: nothing is uploaded and the UI shows
nothing about Paperless. What the document is filed as (type, tags,
correspondent) is in config.yaml, see PaperlessSettings.

The token never leaves this module: it's not logged, not put into error
messages and not shown in the UI.

Paperless consumes uploads asynchronously: post_document returns a task ID,
and the document ID only exists once the task has finished. So the state on
the sale goes pending -> uploaded/failed, and is saved after every step so a
crash or restart leaves a truthful state behind.
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

from . import sales
from .config import Hunter, PaperlessSettings
from .models import PaperlessState, Sale
from .pdf import render_sale_pdf

logger = logging.getLogger(__name__)

TIMEOUT = 20            # per request
TASK_WAIT = 60          # how long to wait for Paperless to consume the file
TASK_POLL = 2

# Paperless's message when the same file was already consumed; the existing
# document's ID follows as "#123" in recent versions.
DUPLICATE = re.compile(r"duplicate", re.I)
DUPLICATE_ID = re.compile(r"#(\d+)")


class PaperlessError(Exception):
    """A failure worth showing to the user. The text never contains the token."""


def settings() -> tuple[str, str] | None:
    """(url, token) from the environment, or None when not configured."""
    url = os.environ.get("PAPERLESS_URL", "").strip().rstrip("/")
    token = os.environ.get("PAPERLESS_TOKEN", "").strip()
    return (url, token) if url and token else None


def enabled() -> bool:
    return settings() is not None


def base_url() -> str:
    s = settings()
    return s[0] if s else ""


def document_url(document_id: int) -> str:
    return f"{base_url()}/documents/{document_id}/details"


# --- HTTP ------------------------------------------------------------------

def _request(method: str, path: str, *, data: bytes | None = None, content_type: str | None = None,
             query: dict | None = None):
    s = settings()
    if s is None:
        raise PaperlessError("Paperless is not configured")
    url, token = s
    full = f"{url}{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
    headers = {"Authorization": f"Token {token}", "Accept": "application/json; version=5"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(full, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        # 401 = the token itself is wrong; 403 = token fine, the user lacks a
        # permission — say which endpoint, so it's clear what to grant
        if e.code == 401:
            raise PaperlessError("HTTP 401: token rejected") from None
        if e.code == 403:
            raise PaperlessError(f"HTTP 403: user lacks permission for {path}") from None
        raise PaperlessError(f"HTTP {e.code} on {path}: {detail}") from None
    except urllib.error.URLError as e:
        # e.reason carries the useful bit: refused, DNS, certificate
        raise PaperlessError(f"{url} unreachable: {e.reason}") from None
    except TimeoutError:
        raise PaperlessError(f"{url} did not answer within {TIMEOUT} s") from None
    return json.loads(body) if body else None


def _get(path: str, **query):
    return _request("GET", path, query=query or None)


def _post_json(path: str, payload: dict):
    return _request("POST", path, data=json.dumps(payload).encode(), content_type="application/json")


def _multipart(fields: list[tuple[str, str]], file_field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n".encode() + content + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# --- metadata --------------------------------------------------------------

# Document types and tags hardly ever change; cache their IDs per process.
# Correspondents (one per buyer) are looked up every time.
_ids: dict[tuple[str, str], int] = {}

MATCH_NONE = 0   # Paperless matching algorithm "none": never auto-assign


def _find_or_create(endpoint: str, name: str, cache: bool = True) -> int:
    key = (endpoint, name.casefold())
    if cache and key in _ids:
        return _ids[key]
    found = _get(f"/api/{endpoint}/", name__iexact=name)
    if found and found.get("results"):
        obj_id = found["results"][0]["id"]
    else:
        # matching "none": otherwise Paperless starts filing unrelated
        # documents under a buyer's name or with our tag
        obj_id = _post_json(f"/api/{endpoint}/", {"name": name, "matching_algorithm": MATCH_NONE})["id"]
    if cache:
        _ids[key] = obj_id
    return obj_id


def title(sale: Sale, lang: str) -> str:
    from .i18n import t
    return t("Rechnung Nr. {number} – {buyer}", lang, number=sale.number, buyer=sale.buyer_name)


# --- state -----------------------------------------------------------------

def _save(sale: Sale, **changes) -> Sale:
    state = (sale.paperless or PaperlessState()).model_copy(update=changes)
    state.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sale = sale.model_copy(update={"paperless": state})
    sales.update(sale)
    return sale


def _check_task(sale: Sale) -> Sale:
    """Ask Paperless how the stored task went and record the answer."""
    tasks = _get("/api/tasks/", task_id=sale.paperless.task_id)
    task = tasks[0] if isinstance(tasks, list) and tasks else None
    if task is None:
        return sale                                 # not known (yet)
    status = task.get("status")
    if status == "SUCCESS" and task.get("related_document"):
        return _save(sale, status="uploaded", document_id=int(task["related_document"]), error=None)
    if status == "FAILURE":
        message = str(task.get("result") or "consumption failed")
        if DUPLICATE.search(message):
            # already in Paperless — the same PDF bytes were consumed before
            m = DUPLICATE_ID.search(message)
            return _save(sale, status="uploaded", document_id=int(m.group(1)) if m else None, error=None)
        return _save(sale, status="failed", error=message[:300])
    return sale                                     # PENDING / STARTED


def refresh(sale: Sale) -> Sale:
    """Update a pending sale from Paperless. For the receipt page: cheap, and
    silently keeps the old state if Paperless can't be reached."""
    if not enabled() or not sale.paperless or sale.paperless.status != "pending" or not sale.paperless.task_id:
        return sale
    try:
        return _check_task(sale)
    except PaperlessError as e:
        logger.info("paperless: could not check task for sale %s: %s", sale.number, e)
        return sale


def upload_sale(sale_id: str, hunter: Hunter | None, options: PaperlessSettings, lang: str) -> None:
    """Upload a sale's invoice. Runs as a background task; never raises."""
    sale = sales.get(sale_id)
    if sale is None or not enabled():
        return
    try:
        # A retry must not make a second document: if an earlier attempt got
        # as far as a task, ask about that one first.
        if sale.paperless and sale.paperless.task_id:
            sale = _check_task(sale)
            if sale.paperless.status == "uploaded":
                return

        fields: list[tuple[str, str]] = [
            ("title", title(sale, lang)),
            ("created", sale.created_at[:10]),
            ("document_type", str(_find_or_create("document_types", options.document_type))),
        ]
        for tag in options.tags:
            fields.append(("tags", str(_find_or_create("tags", tag))))
        if options.buyer_as_correspondent and sale.buyer_name:
            fields.append(("correspondent", str(_find_or_create("correspondents", sale.buyer_name, cache=False))))

        pdf = render_sale_pdf(sale, hunter, lang)
        body, content_type = _multipart(fields, "document", f"rechnung-{sale.number}.pdf", pdf)
        task_id = _request("POST", "/api/documents/post_document/", data=body, content_type=content_type)
        sale = _save(sale, status="pending", task_id=str(task_id), error=None)
        logger.info("paperless: sale %s uploaded, task %s", sale.number, task_id)

        deadline = time.monotonic() + TASK_WAIT
        while time.monotonic() < deadline and sale.paperless.status == "pending":
            time.sleep(TASK_POLL)
            sale = _check_task(sale)
        # Still pending after the wait: Paperless is slow (OCR). The receipt
        # page asks again whenever it's opened.
    except PaperlessError as e:
        logger.warning("paperless: upload of sale %s failed: %s", sale.number, e)
        _save(sale, status="failed", error=str(e)[:300])
    except Exception as e:                          # never kill the worker
        logger.exception("paperless: unexpected error for sale %s", sale.number)
        _save(sale, status="failed", error=type(e).__name__)


# What the upload reads, one line per Paperless permission (docs/paperless.md).
# The test asks exactly these, so "OK" means the upload's reads will work and
# a 403 names the missing one. Creating (add) is only tried on a real upload.
CHECKS = [
    ("/api/document_types/", "Document type: view"),
    ("/api/tags/", "Tag: view"),
    ("/api/correspondents/", "Correspondent: view"),
    ("/api/tasks/", "PaperlessTask: view"),
]


def check_connection() -> tuple[bool, str]:
    """For the settings page: (ok, message). The message never has the token."""
    for path, permission in CHECKS:
        try:
            _get(path, page_size=1)
        except PaperlessError as e:
            if str(e).startswith("HTTP 403"):
                return False, f"HTTP 403: missing permission \"{permission}\""
            return False, str(e)
    return True, ""

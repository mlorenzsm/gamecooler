"""The public stock page as a static bundle, for a web host that runs no code.

The app is LAN-only and has no authentication, so publishing a page means
putting a *copy* somewhere else — a host that holds one HTML file, one
stylesheet and three fonts. Nothing there can reach /settings, /finances or
/parts.json, because the app isn't on that machine at all.

`build()` renders the same template the live /stock route uses (see
`public_stock_groups` in main.py), rewrites the asset paths to be relative and
returns the files as {relative path: bytes}. `app/publish.py` uploads them.

The page is a snapshot: it shows what was in stock when it was built. The app
rebuilds and uploads it after every change, so it is seconds behind rather
than weeks — but it is never a live view.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"
FONTS = ("geist.woff2", "bricolage.woff2", "geist-mono.woff2")

# The stylesheet refers to its fonts as /static/fonts/…; in the bundle they sit
# in fonts/ next to app.css, so the URL must become relative.
_FONT_URL = re.compile(r'url\("/static/fonts/([^"]+)"\)')


def css_for_bundle() -> bytes:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    return _FONT_URL.sub(r'url("fonts/\1")', css).encode("utf-8")


def build(env, groups: list, public_url: str = "", region: str = "",
          order_email: str = "", logo: bytes | None = None) -> dict[str, bytes]:
    """The bundle: {"index.html": …, "app.css": …, "fonts/…": …, ".htaccess": …}.

    `env` is the Jinja environment (templates.env), so the page comes from the
    one template the app itself uses. `logo` is the same PNG the invoice
    letterhead uses (Settings → Rechnungslogo); None means the page has none.
    """
    html = env.get_template("stock_public.html").render(
        groups=groups,
        css_href="app.css",
        favicon_href="favicon.png",
        apple_icon_href="apple-touch-icon.png",
        public_url=public_url,
        region=region,
        order_email=order_email,
        has_logo=logo is not None,
    )
    files: dict[str, bytes] = {"index.html": html.encode("utf-8"), "app.css": css_for_bundle()}
    for name in FONTS:
        path = STATIC / "fonts" / name
        if path.exists():
            files[f"fonts/{name}"] = path.read_bytes()
    # Tab icon and the larger one a phone uses when the page is added to the
    # home screen. Same artwork as the app's own icons.
    for source, target in (("favicon-32.png", "favicon.png"), ("icon-180.png", "apple-touch-icon.png")):
        path = STATIC / source
        if path.exists():
            files[target] = path.read_bytes()
    if logo is not None:
        files["logo.png"] = logo
    # Shared hosting is usually Apache. Harmless elsewhere; without it the
    # directory listing would be open and the page would be indexed.
    files[".htaccess"] = (
        "DirectoryIndex index.html\n"
        "Options -Indexes\n"
        "<IfModule mod_headers.c>\n"
        "  Header set X-Robots-Tag \"noindex, nofollow\"\n"
        "</IfModule>\n"
    ).encode("utf-8")
    return files

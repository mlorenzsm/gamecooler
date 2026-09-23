"""Translations for German and English.

A deliberately small catalog instead of gettext: two languages, no external
translators, and no compile step that the autodeploy would have to run.

The key is the German text itself. That keeps templates readable, and German
needs no second copy — only English has entries in CATALOG["en"]. A German
string without an English entry is a bug; tests/test_i18n.py fails the build
for it.

Placeholders use str.format: t("{n} Teilstücke gespeichert", n=3).
"""

from contextvars import ContextVar
from datetime import date

from .i18n_catalog import EN

LANGUAGES = {"de": "Deutsch", "en": "English"}
DEFAULT = "de"

# The language of the current request. A context variable, so code that has
# no request at hand (labels, PDF, error messages deep in the models) can
# still translate — and concurrent requests don't see each other's language.
_current: ContextVar[str] = ContextVar("lang", default=DEFAULT)


def get_lang() -> str:
    return _current.get()


def set_lang(lang: str):
    return _current.set(lang if lang in LANGUAGES else DEFAULT)


def reset_lang(token) -> None:
    _current.reset(token)


def t(text: str, lang: str | None = None, **values) -> str:
    """Translate German `text` into the current (or given) language."""
    lang = lang or _current.get()
    translated = EN.get(text, text) if lang == "en" else text
    return translated.format(**values) if values else translated


def pick_language(accept_language: str | None) -> str:
    """Best supported language from an Accept-Language header, else German."""
    if not accept_language:
        return DEFAULT
    weighted = []
    for part in accept_language.split(","):
        tag, _, q = part.strip().partition(";q=")
        try:
            weight = float(q) if q else 1.0
        except ValueError:
            weight = 0.0
        weighted.append((weight, tag.strip().lower()[:2]))
    for _, code in sorted(weighted, key=lambda w: -w[0]):
        if code in LANGUAGES:
            return code
    return DEFAULT


# --- numbers and dates ------------------------------------------------------
#
# German writes 1,5 and 23.09.2026. English uses a decimal point, and dates
# with the month as a word ("23 Sep 2026"): 09/23 vs 23/09 is ambiguous
# between US and UK readers, a month name isn't.

def format_number(value: float | None, decimals: int = 2, lang: str | None = None) -> str:
    if value is None:
        return "–"
    text = f"{value:.{decimals}f}"
    if (lang or _current.get()) == "de":
        text = text.replace(".", ",")
    return text


def decimal_separator(lang: str | None = None) -> str:
    return "," if (lang or _current.get()) == "de" else "."


_MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_date(d: date, lang: str | None = None) -> str:
    if (lang or _current.get()) == "de":
        return d.strftime("%d.%m.%Y")
    return f"{d.day} {_MONTHS_EN[d.month - 1]} {d.year}"

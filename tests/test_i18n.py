"""Checks the English catalog against the texts the app actually uses.

Run it with:

    .venv/bin/python tests/test_i18n.py

No test framework needed; exits non-zero on a problem, so the autodeploy's
health or a pre-push hook can use it. It fails when

- a t("…") text in the code or templates has no English entry,
- an English entry's {placeholders} differ from the German text's,
- an entry in the catalog is no longer used anywhere (dead translation).
"""

import re
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.i18n_catalog import EN  # noqa: E402

# t("…") / t('…') in Python and Jinja, first argument only. Adjacent string
# literals aren't used for keys, so a single literal is enough to match.
CALL = re.compile(r"""\bt\(\s*(?P<q>["'])(?P<text>(?:\\.|(?!(?P=q)).)*)(?P=q)""", re.S)
# German keys that reach t() through a variable rather than a literal:
# part-kind headings (PART_KINDS), PDF column headers, units.
INDIRECT = {"Teilstücke", "Zubereitungen", "Pos.", "Artikel", "Menge", "€/kg", "Preis", "ID", "Stk."}


def used_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    files = list((ROOT / "app").glob("*.py")) + list((ROOT / "app" / "templates").glob("*.html"))
    for path in files:
        if path.name in ("i18n.py", "i18n_catalog.py"):
            continue
        for m in CALL.finditer(path.read_text(encoding="utf-8")):
            # Undo the escapes Python and Jinja apply to string literals: an
            # escaped quote, and \n — a key may contain a real line break.
            text = m.group("text").replace("\\" + m.group("q"), m.group("q")).replace("\\n", "\n")
            keys.setdefault(text, str(path.relative_to(ROOT)))
    for k in INDIRECT:
        keys.setdefault(k, "(indirect)")
    return keys


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def main() -> int:
    used = used_keys()
    problems = []
    for text, where in sorted(used.items()):
        if text not in EN:
            problems.append(f"missing English: {text!r}  ({where})")
        elif placeholders(text) != placeholders(EN[text]):
            problems.append(f"placeholder mismatch: {text!r} {sorted(placeholders(text))} "
                            f"vs {EN[text]!r} {sorted(placeholders(EN[text]))}")
    for text in sorted(set(EN) - set(used)):
        problems.append(f"unused catalog entry: {text!r}")
    for p in problems:
        print(p)
    print(f"{len(used)} texts, {len(EN)} catalog entries, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

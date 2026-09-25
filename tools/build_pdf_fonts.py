"""Builds the TTF fonts the invoice PDF uses from the web fonts in
app/static/fonts. Run once after changing a web font:

    uv run --no-project --with fonttools --with brotli python tools/build_pdf_fonts.py

Why: fpdf2 reads TrueType, not woff2, and uses one file per weight rather
than a variable font. So each weight the PDF needs is cut out of the
variable font as a static TTF.

The body font also gets its tabular digits baked in: without a text shaper
fpdf2 can't switch on the OpenType feature "tnum", and proportional digits
make a column of prices wobble. Both fonts are under the SIL Open Font
License (see app/fonts/LICENSE-*.txt), which allows this; neither declares
a Reserved Font Name.
"""

from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app" / "static" / "fonts"
DST = ROOT / "app" / "fonts"

# (source, weight, output, bake tabular digits)
INSTANCES = [
    ("geist.woff2", 400, "Geist-Regular.ttf", True),
    ("geist.woff2", 600, "Geist-SemiBold.ttf", True),
    ("bricolage.woff2", 700, "Bricolage-Bold.ttf", False),
    ("geist-mono.woff2", 400, "GeistMono-Regular.ttf", False),
]


def bake_tabular_digits(font: TTFont) -> None:
    """Point the cmap for 0–9 at the glyphs the "tnum" feature would use."""
    gsub = font["GSUB"].table
    lookups = {i for fr in gsub.FeatureList.FeatureRecord if fr.FeatureTag == "tnum"
               for i in fr.Feature.LookupListIndex}
    mapping = {}
    for i in lookups:
        for sub in gsub.LookupList.Lookup[i].SubTable:
            mapping.update(getattr(sub, "mapping", {}))
    for table in font["cmap"].tables:
        for code in range(ord("0"), ord("9") + 1):
            glyph = table.cmap.get(code)
            if glyph in mapping:
                table.cmap[code] = mapping[glyph]


for src, weight, out, tabular in INSTANCES:
    font = instantiateVariableFont(TTFont(SRC / src), {"wght": weight})
    if tabular:
        bake_tabular_digits(font)
    font.flavor = None                      # woff2 -> plain TrueType
    font.save(DST / out)
    print(f"{out:24} {(DST / out).stat().st_size // 1024} KB")

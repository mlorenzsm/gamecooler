from datetime import date

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_M

from .config import FONTS_DIR, Hunter
from .models import PartRecord, format_amount, format_de

# Printable area of the 39x90 die-cut label at 300 dpi, rendered landscape
WIDTH = 991
HEIGHT = 413
MARGIN = 24

FONT_REGULAR = FONTS_DIR / "DejaVuSans.ttf"
FONT_BOLD = FONTS_DIR / "DejaVuSans-Bold.ttf"
ICONS_DIR = FONTS_DIR.parent / "icons"


def _species_icon(species: str, size: int) -> Image.Image | None:
    path = ICONS_DIR / f"{species.lower().replace('/', '-')}.png"
    if not path.exists():
        return None
    icon = Image.open(path).convert("RGBA")
    white = Image.new("RGBA", icon.size, (255, 255, 255, 255))
    white.alpha_composite(icon)
    gray = white.convert("L").resize((size, size), Image.LANCZOS)
    return gray.point(lambda p: 0 if p < 160 else 255, mode="1")


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_BOLD if bold else FONT_REGULAR), size)


def _format_date(iso_timestamp: str) -> str:
    return date.fromisoformat(iso_timestamp[:10]).strftime("%d.%m.%Y")


def _best_before(iso_timestamp: str, months: int) -> str:
    d = date.fromisoformat(iso_timestamp[:10])
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    try:
        d = d.replace(year=year, month=month)
    except ValueError:  # e.g. 31.03. + 11 months -> 31.02. doesn't exist
        d = d.replace(year=year, month=month, day=1)
    return d.strftime("%d.%m.%Y")


def _fit_text(draw: ImageDraw.ImageDraw, text: str, size: int, max_width: int,
              bold: bool = False, min_size: int = 20) -> ImageFont.FreeTypeFont:
    font = _font(size, bold)
    while draw.textlength(text, font=font) > max_width and font.size > min_size:
        font = _font(font.size - 2, bold)
    return font


# Font sizes and line spacing of the info label. With ingredients the label
# needs room for a small text block at the bottom, so everything above shrinks;
# without them the label stays exactly as it was.
_INFO_NORMAL = dict(icon=72, title=52, grid=36, grid_label_width=185, grid_top=86,
                    grid_step=62, contact1=30, contact2=28, contact_step=44)
_INFO_COMPACT = dict(icon=52, title=40, grid=28, grid_label_width=140, grid_top=62,
                     grid_step=40, contact1=24, contact2=22, contact_step=30)

# Smallest ingredients font. EU food labelling (LMIV Art. 13) asks for an
# x-height of at least 1.2 mm, or 0.9 mm on small packages; with DejaVu Sans at
# 300 dpi that is about 26 px and 20 px. Below 20 px it is no longer legal.
INGREDIENTS_MAX = 24
INGREDIENTS_MIN = 20


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_width: int) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if line and draw.textlength(candidate, font=font) > max_width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def _fit_block(draw: ImageDraw.ImageDraw, text: str, max_width: int, max_height: int
               ) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """Largest font from INGREDIENTS_MAX down whose wrapped text fits the box.

    Returns the font, the lines and the line height. If even the smallest font
    doesn't fit, the text is cut at the last line that fits and ends in "…" —
    better than printing it past the label edge.
    """
    for size in range(INGREDIENTS_MAX, INGREDIENTS_MIN - 1, -1):
        font = _font(size)
        step = int(size * 1.2)
        lines = _wrap(draw, text, font, max_width)
        if len(lines) * step <= max_height:
            return font, lines, step
    font = _font(INGREDIENTS_MIN)
    step = int(INGREDIENTS_MIN * 1.2)
    lines = _wrap(draw, text, font, max_width)[: max(1, max_height // step)]
    while lines and draw.textlength(lines[-1] + " …", font=font) > max_width:
        lines[-1] = lines[-1].rsplit(" ", 1)[0] if " " in lines[-1] else lines[-1][:-1]
    lines[-1] += " …"
    return font, lines, step


def render_info_label(record: PartRecord, hunter: Hunter | None = None) -> Image.Image:
    img = Image.new("1", (WIDTH, HEIGHT), 1)
    draw = ImageDraw.Draw(img)
    inner_width = WIDTH - 2 * MARGIN
    s = _INFO_COMPACT if record.ingredients else _INFO_NORMAL

    title = f"{record.species} – {record.part}"
    icon_size = s["icon"]
    icon = _species_icon(record.species, icon_size)
    title_x = MARGIN
    if icon is not None:
        img.paste(icon, (MARGIN, MARGIN - 8))
        title_x += icon_size + 16
    title_font = _fit_text(draw, title, s["title"], WIDTH - MARGIN - title_x, bold=True, min_size=26)
    draw.text((title_x, MARGIN - 4), title, font=title_font, fill=0)

    # 2x2 grid: Gewicht / Preis/kg then Datum / Preis. Weight and price depend on
    # each other (total = weight x price), so both drop out if either is unset.
    date_cell = ("Datum", _format_date(record.created_at), False)
    if record.pieces is not None:
        # Counted parts: the piece count is on the QR label, and the price is
        # fixed rather than weight x price/kg — so only the price is shown.
        if record.total_price is not None:
            grid = [(date_cell, ("Preis", f"{format_de(record.total_price)} €", True))]
        else:
            grid = [(date_cell,)]
    elif record.weight_kg is not None and record.price_per_kg is not None:
        grid = [
            (("Gewicht", f"{format_de(record.weight_kg, 3)} kg", False),
             ("Preis/kg", f"{format_de(record.price_per_kg)} €", False)),
            (date_cell, ("Preis", f"{format_de(record.total_price or 0.0)} €", True)),
        ]
    elif record.weight_kg is not None:
        grid = [(("Gewicht", f"{format_de(record.weight_kg, 3)} kg", False), date_cell)]
    else:
        grid = [(date_cell,)]

    label_font = _font(s["grid"])
    col_x = (MARGIN, MARGIN + inner_width // 2)
    label_width = s["grid_label_width"]
    y = MARGIN + s["grid_top"]
    for row in grid:
        for (name, value, bold), x in zip(row, col_x):
            draw.text((x, y), f"{name}:", font=label_font, fill=0)
            draw.text((x + label_width, y), value, font=_font(s["grid"], bold=bold), fill=0)
        y += s["grid_step"]

    # contact block
    y += 6 if record.ingredients else 10
    draw.line((MARGIN, y, WIDTH - MARGIN, y), fill=0, width=2)
    y += 8 if record.ingredients else 12
    if hunter is None:
        name_font = _fit_text(draw, record.hunter, s["contact1"] + 4, inner_width, bold=True)
        draw.text((MARGIN, y), record.hunter, font=name_font, fill=0)
        y += s["contact_step"]
    else:
        line1 = f"{hunter.name} · {hunter.address}"
        line2 = f"Tel. {hunter.phone}"
        if hunter.email:
            line2 += f" · {hunter.email}"
        line1_font = _fit_text(draw, line1, s["contact1"], inner_width, bold=True, min_size=20)
        draw.text((MARGIN, y), line1, font=line1_font, fill=0)
        y += s["contact_step"]
        line2_font = _fit_text(draw, line2, s["contact2"], inner_width, min_size=20)
        draw.text((MARGIN, y), line2, font=line2_font, fill=0)
        y += s["contact_step"]

    if record.ingredients:
        y += 6
        draw.line((MARGIN, y, WIDTH - MARGIN, y), fill=0, width=1)
        y += 6
        text = f"Zutaten: {record.ingredients}"
        font, lines, step = _fit_block(draw, text, inner_width, HEIGHT - MARGIN - y)
        for line in lines:
            draw.text((MARGIN, y), line, font=font, fill=0)
            y += step

    return img


def render_qr_label(record: PartRecord, best_before_months: int = 12) -> Image.Image:
    img = Image.new("1", (WIDTH, HEIGHT), 1)
    draw = ImageDraw.Draw(img)

    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(record.uuid)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").get_image()
    qr_size = HEIGHT - 2 * MARGIN
    qr_img = qr_img.resize((qr_size, qr_size), Image.NEAREST)
    img.paste(qr_img, (MARGIN, MARGIN))

    text_x = MARGIN + qr_size + 40
    short_id = record.uuid.split("-")[0].upper()
    draw.text((text_x, MARGIN + 10), short_id, font=_font(72, bold=True), fill=0)

    # Next to the QR code there is only about half the label width. Long part
    # names (Bratwurst "Salsiccia Art") don't fit on one line with the species,
    # so the part moves to its own line; there is room for one extra line
    # above the UUID. Anything still too wide is shrunk rather than cut off.
    text_width = WIDTH - MARGIN - text_x
    info_font = _font(32)
    title = f"{record.species} – {record.part}"
    if draw.textlength(title, font=info_font) <= text_width:
        lines = [title]
    else:
        lines = [record.species, record.part]
    if record.pieces is not None or record.weight_kg is not None:
        lines.append(format_amount(record))
    lines.append(f"Mind. haltbar bis: {_best_before(record.created_at, best_before_months)}")

    y = MARGIN + 120
    for line in lines:
        draw.text((text_x, y), line, font=_fit_text(draw, line, 32, text_width, min_size=22), fill=0)
        y += 48

    uuid_font = _font(20)
    draw.text((text_x, HEIGHT - MARGIN - 28), record.uuid, font=uuid_font, fill=0)

    return img

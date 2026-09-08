from datetime import date

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_M

from .config import FONTS_DIR, Hunter
from .models import PartRecord, format_de

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


def render_info_label(record: PartRecord, hunter: Hunter | None = None) -> Image.Image:
    img = Image.new("1", (WIDTH, HEIGHT), 1)
    draw = ImageDraw.Draw(img)
    inner_width = WIDTH - 2 * MARGIN

    title = f"{record.species} – {record.part}"
    icon_size = 72
    icon = _species_icon(record.species, icon_size)
    title_x = MARGIN
    if icon is not None:
        img.paste(icon, (MARGIN, MARGIN - 8))
        title_x += icon_size + 16
    title_font = _fit_text(draw, title, 52, WIDTH - MARGIN - title_x, bold=True, min_size=30)
    draw.text((title_x, MARGIN - 4), title, font=title_font, fill=0)

    # 2x2 grid: Gewicht / Datum | Preis/kg / Preis
    grid = [
        (("Gewicht", f"{format_de(record.weight_kg, 3)} kg", False),
         ("Preis/kg", f"{format_de(record.price_per_kg)} €", False)),
        (("Datum", _format_date(record.created_at), False),
         ("Preis", f"{format_de(record.total_price)} €", True)),
    ]
    label_font = _font(36)
    col_x = (MARGIN, MARGIN + inner_width // 2)
    label_width = 185
    y = MARGIN + 86
    for row in grid:
        for (name, value, bold), x in zip(row, col_x):
            draw.text((x, y), f"{name}:", font=label_font, fill=0)
            draw.text((x + label_width, y), value, font=_font(36, bold=bold), fill=0)
        y += 62

    # contact block
    y += 10
    draw.line((MARGIN, y, WIDTH - MARGIN, y), fill=0, width=2)
    y += 12
    if hunter is None:
        name_font = _fit_text(draw, record.hunter, 34, inner_width, bold=True)
        draw.text((MARGIN, y), record.hunter, font=name_font, fill=0)
        return img

    line1 = f"{hunter.name} · {hunter.address}"
    line2 = f"Tel. {hunter.phone}"
    if hunter.email:
        line2 += f" · {hunter.email}"
    line1_font = _fit_text(draw, line1, 30, inner_width, bold=True, min_size=22)
    draw.text((MARGIN, y), line1, font=line1_font, fill=0)
    y += 44
    line2_font = _fit_text(draw, line2, 28, inner_width, min_size=22)
    draw.text((MARGIN, y), line2, font=line2_font, fill=0)

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

    info_font = _font(32)
    y = MARGIN + 120
    for line in (
        f"{record.species} – {record.part}",
        f"{format_de(record.weight_kg, 3)} kg",
        f"Mind. haltbar bis: {_best_before(record.created_at, best_before_months)}",
    ):
        draw.text((text_x, y), line, font=info_font, fill=0)
        y += 48

    uuid_font = _font(20)
    draw.text((text_x, HEIGHT - MARGIN - 28), record.uuid, font=uuid_font, fill=0)

    return img

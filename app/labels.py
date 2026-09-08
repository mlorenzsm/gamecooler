from datetime import date

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_M

from .config import FONTS_DIR
from .models import PartRecord, format_de

# Printable area of the 39x90 die-cut label at 300 dpi, rendered landscape
WIDTH = 991
HEIGHT = 413
MARGIN = 24

FONT_REGULAR = FONTS_DIR / "DejaVuSans.ttf"
FONT_BOLD = FONTS_DIR / "DejaVuSans-Bold.ttf"


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_BOLD if bold else FONT_REGULAR), size)


def _format_date(iso_timestamp: str) -> str:
    return date.fromisoformat(iso_timestamp[:10]).strftime("%d.%m.%Y")


def render_info_label(record: PartRecord) -> Image.Image:
    img = Image.new("1", (WIDTH, HEIGHT), 1)
    draw = ImageDraw.Draw(img)

    title = f"{record.species} – {record.part}"
    title_font = _font(56, bold=True)
    while draw.textlength(title, font=title_font) > WIDTH - 2 * MARGIN and title_font.size > 30:
        title_font = _font(title_font.size - 4, bold=True)
    draw.text((MARGIN, MARGIN), title, font=title_font, fill=0)

    rows = [
        ("Jäger", record.hunter, False),
        ("Gewicht", f"{format_de(record.weight_kg, 3)} kg", False),
        ("Preis/kg", f"{format_de(record.price_per_kg)} €", False),
        ("Preis", f"{format_de(record.total_price)} €", True),
        ("Datum", _format_date(record.created_at), False),
    ]

    label_font = _font(38)
    y = MARGIN + 76
    row_height = (HEIGHT - y - MARGIN) // len(rows)
    value_x = MARGIN + 260
    for name, value, bold in rows:
        value_font = _font(38, bold=bold)
        draw.text((MARGIN, y), f"{name}:", font=label_font, fill=0)
        draw.text((value_x, y), value, font=value_font, fill=0)
        y += row_height

    return img


def render_qr_label(record: PartRecord) -> Image.Image:
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
        _format_date(record.created_at),
    ):
        draw.text((text_x, y), line, font=info_font, fill=0)
        y += 48

    uuid_font = _font(20)
    draw.text((text_x, HEIGHT - MARGIN - 28), record.uuid, font=uuid_font, fill=0)

    return img

import logging
from datetime import datetime

from fpdf import FPDF

logging.getLogger("fontTools").setLevel(logging.WARNING)

from .config import FONTS_DIR, LOGO_PATH, Hunter
from .i18n import format_date, t
from .models import Sale, format_amount, format_de

# The app's palette ("Field kit", app/static/app.css), in print terms. Green
# is spent in one place only — the total — so the page stays calm.
INK = (0x1A, 0x20, 0x19)
MUTED = (0x5B, 0x64, 0x57)
FAINT = (0x85, 0x8D, 0x81)
LINE = (0xDC, 0xE0, 0xD5)
ACCENT = (0x2E, 0x7A, 0x35)
ACCENT_SOFT = (0xE2, 0xEE, 0xDF)

# A4 in mm. Positions follow DIN 5008 form B, so the buyer address sits in
# the window of a DL envelope when the sheet is folded at the marks.
LEFT, RIGHT = 20, 190
WIDTH = RIGHT - LEFT
ADDRESS_TOP = 45            # address field: 45 mm from the top, 85 mm wide
ADDRESS_WIDTH = 85
INFO_LEFT = 125             # information block next to the address field
FOLD_MARKS = (105, 210)     # fold into thirds
LOGO_TOP, LOGO_SIZE = 9, 25 # letterhead logo, mm; ends just above the rule at 36
HOLE_MARK = 148.5           # centre, for the hole punch

# Item table: position, item, amount, €/kg, amount due.
COLS = (10, 86, 26, 22, 26)
ROW_HEIGHT = 11


class InvoicePDF(FPDF):
    def __init__(self, footer_text: str, page_text: str):
        super().__init__(format="A4")
        self.footer_text = footer_text
        self.page_text = page_text          # "Seite {page} von {nb}"
        # Static cuts of the web fonts (tools/build_pdf_fonts.py); Geist has
        # tabular digits baked in so price columns line up.
        self.add_font("geist", "", FONTS_DIR / "Geist-Regular.ttf")
        self.add_font("geist", "B", FONTS_DIR / "Geist-SemiBold.ttf")
        self.add_font("bricolage", "B", FONTS_DIR / "Bricolage-Bold.ttf")
        self.add_font("mono", "", FONTS_DIR / "GeistMono-Regular.ttf")
        self.set_margins(LEFT, 20, 210 - RIGHT)
        self.set_auto_page_break(True, margin=28)

    def footer(self):
        self.set_draw_color(*LINE)
        self.line(LEFT, 280, RIGHT, 280)
        self.set_xy(LEFT, 282)
        self.style(7.5, MUTED)
        self.cell(WIDTH - 30, 4, self.footer_text)
        # {nb} is fpdf2's alias for the page count, filled in on output
        self.cell(30, 4, self.page_text.format(page=self.page_no(), nb="{nb}"), align="R")

    def style(self, size: float, color=INK, family: str = "geist", bold: bool = False, spacing: float = 0):
        self.set_font(family, "B" if bold else "", size)
        self.set_text_color(*color)
        self.set_char_spacing(spacing)

    def fit(self, text: str, width: float) -> str:
        """Cut text with an ellipsis so it fits width at the current font."""
        if self.get_string_width(text) <= width:
            return text
        while text and self.get_string_width(text + "…") > width:
            text = text[:-1]
        return text.rstrip() + "…"

    def eyebrow(self, text: str, x: float, y: float, w: float = 60, align: str = "L"):
        """Small spaced capitals above a block, like the web app's table heads."""
        self.set_xy(x, y)
        self.style(7, MUTED, bold=True, spacing=0.6)
        self.cell(w, 4, text.upper(), align=align)
        self.set_char_spacing(0)


def render_sale_pdf(sale: Sale, hunter: Hunter | None, lang: str = "de") -> bytes:
    """Invoice / delivery note. `lang` is the label language setting, like the
    labels: the document goes to the buyer, not the person clicking."""
    address = hunter.address if hunter else ""
    phone = t("Tel. {phone}", lang, phone=hunter.phone) if hunter and hunter.phone else ""
    email = hunter.email if hunter else ""
    date = format_date(datetime.fromisoformat(sale.created_at).date(), lang)

    # The footer names the document, not the seller again: that's what a
    # second page needs to be matched to the first.
    pdf = InvoicePDF(
        footer_text=f"{t('Rechnung / Lieferschein', lang)} {t('Nr.', lang)} {sale.number} · {date}",
        page_text=t("Seite {page} von {pages}", lang, page="{page}", pages="{nb}"),
    )
    # Same sale, same bytes: the creation date is the sale's, not "now". A
    # re-upload is then an exact duplicate, which Paperless recognises by
    # checksum and refuses — a second guard against double documents.
    pdf.set_creation_date(datetime.fromisoformat(sale.created_at))
    pdf.add_page()

    # fold and hole-punch marks on the left edge
    pdf.set_draw_color(*FAINT)
    pdf.set_line_width(0.2)
    for y in FOLD_MARKS:
        pdf.line(4, y, 9, y)
    pdf.line(4, HOLE_MARK, 11, HOLE_MARK)

    # --- header: the seller -----------------------------------------------
    # With a logo (Settings -> General) it stands at the left and the name
    # moves next to it; the header keeps its height either way.
    name_x = LEFT
    if LOGO_PATH.exists():
        pdf.image(str(LOGO_PATH), x=LEFT, y=LOGO_TOP, h=LOGO_SIZE, keep_aspect_ratio=True)
        name_x = LEFT + LOGO_SIZE + 4
    pdf.set_xy(name_x, 18)
    pdf.style(17, INK, family="bricolage", bold=True)
    pdf.cell(WIDTH / 2 - (name_x - LEFT), 8, pdf.fit(sale.hunter, WIDTH / 2 - (name_x - LEFT)))
    pdf.set_xy(LEFT + WIDTH / 2, 18.5)
    pdf.style(8.5, MUTED)
    pdf.multi_cell(WIDTH / 2, 4.2, "\n".join(x for x in (address, phone, email) if x), align="R")
    pdf.set_draw_color(*LINE)
    pdf.set_line_width(0.3)
    pdf.line(LEFT, 36, RIGHT, 36)

    # --- address field (window envelope) ----------------------------------
    # the return line sits right above the address, small, so it shows in the
    # window too — the post office's way of knowing where it came from
    pdf.set_xy(LEFT, ADDRESS_TOP + 5)
    pdf.style(6.5, FAINT)
    pdf.cell(ADDRESS_WIDTH, 3, pdf.fit(" · ".join(x for x in (sale.hunter, address) if x), ADDRESS_WIDTH))
    pdf.set_draw_color(*LINE)
    pdf.line(LEFT, ADDRESS_TOP + 8.5, LEFT + ADDRESS_WIDTH - 10, ADDRESS_TOP + 8.5)
    pdf.set_xy(LEFT, ADDRESS_TOP + 17.7)
    pdf.style(10.5, INK, bold=True)
    pdf.cell(ADDRESS_WIDTH, 5.2, sale.buyer_name, new_x="LMARGIN", new_y="NEXT")
    pdf.style(10.5, INK)
    pdf.multi_cell(ADDRESS_WIDTH, 5.2, sale.buyer_address)

    # --- information block ------------------------------------------------
    # Only the date: the number is in the title, and the seller is the
    # letterhead — a "Seller" row would repeat the name a third time.
    info = [
        (t("Datum", lang), date),
    ]
    y = ADDRESS_TOP + 5
    for label, value in info:
        pdf.set_xy(INFO_LEFT, y)
        pdf.style(8.5, MUTED)
        pdf.cell(22, 5.5, label)
        pdf.style(9.5, INK)
        pdf.cell(RIGHT - INFO_LEFT - 22, 5.5, pdf.fit(value, RIGHT - INFO_LEFT - 22), align="R")
        y += 5.5
    if sale.delivery_address.strip() != sale.buyer_address.strip():
        y += 4
        pdf.eyebrow(t("Lieferadresse", lang), INFO_LEFT, y)
        pdf.set_xy(INFO_LEFT, y + 5)
        pdf.style(9.5, INK)
        pdf.multi_cell(RIGHT - INFO_LEFT, 4.8, sale.delivery_address)

    # --- title ------------------------------------------------------------
    pdf.set_xy(LEFT, 100)
    pdf.style(20, INK, family="bricolage", bold=True)
    title = t("Rechnung / Lieferschein", lang)
    pdf.cell(pdf.get_string_width(title) + 3, 10, title)
    pdf.style(20, FAINT, family="bricolage", bold=True)
    pdf.cell(40, 10, f"{t('Nr.', lang)} {sale.number}")
    pdf.set_y(116)

    # --- items ------------------------------------------------------------
    heads = [(t("Pos.", lang), "R"), (t("Artikel", lang), "L"), (t("Menge", lang), "R"),
             (t("€/kg", lang), "R"), (t("Betrag", lang), "R")]

    def table_head():
        y = pdf.get_y()
        x = LEFT
        for (text, align), w in zip(heads, COLS):
            pdf.eyebrow(text, x + (2 if align == "L" else 0), y, w - (2 if align == "L" else 0), align)
            x += w
        pdf.set_draw_color(*INK)
        pdf.set_line_width(0.35)
        pdf.line(LEFT, y + 6, RIGHT, y + 6)
        pdf.set_y(y + 6)

    table_head()
    pdf.set_line_width(0.2)
    for i, item in enumerate(sale.items, start=1):
        if pdf.get_y() + ROW_HEIGHT > pdf.page_break_trigger:
            pdf.add_page()
            pdf.set_y(20)
            table_head()
            pdf.set_line_width(0.2)
        y = pdf.get_y()
        x = LEFT
        # position
        pdf.set_xy(x, y + 2.2)
        pdf.style(9, FAINT)
        pdf.cell(COLS[0], 4.5, str(i), align="R")
        x += COLS[0]
        # item: the cut, then species and label ID — the ID is what's printed
        # on the QR label, so the buyer can match package and line
        pdf.set_xy(x + 2, y + 2)
        pdf.style(10, INK, bold=True)
        pdf.cell(COLS[1] - 2, 4.5, pdf.fit(item.part, COLS[1] - 4))
        pdf.set_xy(x + 2, y + 6.3)
        pdf.style(8, MUTED)
        species = f"{item.species} · "
        pdf.cell(pdf.get_string_width(species), 3.5, species)
        pdf.style(7.5, MUTED, family="mono")
        pdf.cell(30, 3.5, item.uuid[:8].upper())
        x += COLS[1]
        # numbers, on the first line
        has_amount = item.weight_kg is not None or item.pieces is not None
        for w, text, color, bold in (
            (COLS[2], format_amount(item, lang) if has_amount else "–", INK if has_amount else FAINT, False),
            (COLS[3], format_de(item.price_per_kg, 2, lang), MUTED if item.price_per_kg is not None else FAINT, False),
            (COLS[4], f"{format_de(item.total_price, 2, lang)} €", INK, True),
        ):
            pdf.set_xy(x, y + 2)
            pdf.style(10, color, bold=bold)
            pdf.cell(w, 4.5, text, align="R")
            x += w
        pdf.set_draw_color(*LINE)
        pdf.line(LEFT, y + ROW_HEIGHT, RIGHT, y + ROW_HEIGHT)
        pdf.set_y(y + ROW_HEIGHT)

    # --- total ------------------------------------------------------------
    if pdf.get_y() + 34 > pdf.page_break_trigger:
        pdf.add_page()
        pdf.set_y(20)
    y = pdf.get_y() + 5
    box_w = 78
    pdf.set_fill_color(*ACCENT_SOFT)
    pdf.rect(RIGHT - box_w, y, box_w, 15, style="F", round_corners=True, corner_radius=2.5)
    pdf.set_xy(RIGHT - box_w + 5, y + 5.5)
    pdf.style(9.5, ACCENT, bold=True)
    count = t("{n} Teilstücke", lang, n=len(sale.items))
    pdf.cell(30, 4.5, t("Gesamt", lang))
    pdf.set_xy(RIGHT - box_w + 5, y + 9.3)
    pdf.style(7.5, ACCENT)
    pdf.cell(30, 3.5, count)
    pdf.set_xy(RIGHT - 45, y + 3.8)
    pdf.style(17, ACCENT, family="bricolage", bold=True)
    pdf.cell(40, 8, f"{format_de(sale.total, 2, lang)} €", align="R")

    # --- notes: next to the total, where the eye lands last ----------------
    pdf.set_xy(LEFT, y + 2)
    pdf.style(8.5, MUTED)
    pdf.multi_cell(WIDTH - box_w - 10, 4.4, t("Wildbret aus eigener Jagd. Kein Ausweis der Umsatzsteuer (Kleinunternehmerregelung, § 19 UStG).", lang), align="L")

    return bytes(pdf.output())

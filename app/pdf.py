import logging
from datetime import datetime

from fpdf import FPDF

logging.getLogger("fontTools").setLevel(logging.WARNING)

from .config import FONTS_DIR, Hunter
from .i18n import format_date, t
from .models import Sale, format_amount, format_de


def render_sale_pdf(sale: Sale, hunter: Hunter | None, lang: str = "de") -> bytes:
    """Invoice / delivery note. `lang` is the label language setting, like the
    labels: the document goes to the buyer, not the person clicking."""
    pdf = FPDF(format="A4")
    pdf.add_font("dejavu", "", FONTS_DIR / "DejaVuSans.ttf")
    pdf.add_font("dejavu", "B", FONTS_DIR / "DejaVuSans-Bold.ttf")
    pdf.set_auto_page_break(True, margin=20)
    pdf.add_page()

    # seller
    pdf.set_font("dejavu", "B", 11)
    pdf.cell(0, 6, sale.hunter, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("dejavu", "", 10)
    if hunter:
        pdf.cell(0, 5, hunter.address, new_x="LMARGIN", new_y="NEXT")
        contact = " · ".join(x for x in (t("Tel. {phone}", lang, phone=hunter.phone) if hunter.phone else "", hunter.email) if x)
        if contact:
            pdf.cell(0, 5, contact, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)

    # buyer / delivery address
    y = pdf.get_y()
    pdf.set_font("dejavu", "B", 10)
    pdf.cell(90, 5, t("Käufer", lang), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("dejavu", "", 10)
    pdf.multi_cell(90, 5, f"{sale.buyer_name}\n{sale.buyer_address}")
    pdf.set_xy(110, y)
    pdf.set_font("dejavu", "B", 10)
    pdf.cell(90, 5, t("Lieferadresse", lang), new_x="LEFT", new_y="NEXT")
    pdf.set_font("dejavu", "", 10)
    pdf.multi_cell(90, 5, sale.delivery_address)
    pdf.ln(10)

    pdf.set_font("dejavu", "B", 16)
    pdf.cell(0, 10, t("Rechnung / Lieferschein Nr. {number}", lang, number=sale.number), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("dejavu", "", 10)
    pdf.cell(0, 6, t("Datum: {date}", lang, date=format_date(datetime.fromisoformat(sale.created_at).date(), lang)), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # items table
    widths = (12, 66, 24, 24, 26, 26)
    headers = tuple(t(h, lang) for h in ("Pos.", "Artikel", "Menge", "€/kg", "Preis", "ID"))
    aligns = ("R", "L", "R", "R", "R", "L")
    pdf.set_font("dejavu", "B", 10)
    for w, h, a in zip(widths, headers, aligns):
        pdf.cell(w, 7, h, border="B", align=a)
    pdf.ln()
    pdf.set_font("dejavu", "", 10)
    for i, item in enumerate(sale.items, start=1):
        cells = (
            str(i),
            f"{item.species} – {item.part}",
            format_amount(item, lang),
            format_de(item.price_per_kg, 2, lang),
            f"{format_de(item.total_price, 2, lang)} €",
            item.uuid[:8].upper(),
        )
        for w, c, a in zip(widths, cells, aligns):
            pdf.cell(w, 7, c, border="B", align=a)
        pdf.ln()

    pdf.set_font("dejavu", "B", 11)
    pdf.cell(sum(widths[:4]), 9, t("Gesamt", lang), align="R")
    pdf.cell(widths[4], 9, f"{format_de(sale.total, 2, lang)} €", align="R")
    pdf.ln(16)

    pdf.set_font("dejavu", "", 9)
    pdf.multi_cell(0, 5, t("Wildbret aus eigener Jagd. Kein Ausweis der Umsatzsteuer (Kleinunternehmerregelung, § 19 UStG).", lang))

    return bytes(pdf.output())

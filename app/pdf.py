import logging
from datetime import datetime

from fpdf import FPDF

logging.getLogger("fontTools").setLevel(logging.WARNING)

from .config import FONTS_DIR, Hunter
from .models import Sale, format_amount, format_de


def _format_date(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d.%m.%Y")


def render_sale_pdf(sale: Sale, hunter: Hunter | None) -> bytes:
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
        contact = " · ".join(x for x in (f"Tel. {hunter.phone}" if hunter.phone else "", hunter.email) if x)
        if contact:
            pdf.cell(0, 5, contact, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)

    # buyer / delivery address
    y = pdf.get_y()
    pdf.set_font("dejavu", "B", 10)
    pdf.cell(90, 5, "Käufer", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("dejavu", "", 10)
    pdf.multi_cell(90, 5, f"{sale.buyer_name}\n{sale.buyer_address}")
    pdf.set_xy(110, y)
    pdf.set_font("dejavu", "B", 10)
    pdf.cell(90, 5, "Lieferadresse", new_x="LEFT", new_y="NEXT")
    pdf.set_font("dejavu", "", 10)
    pdf.multi_cell(90, 5, sale.delivery_address)
    pdf.ln(10)

    pdf.set_font("dejavu", "B", 16)
    pdf.cell(0, 10, f"Rechnung / Lieferschein Nr. {sale.number}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("dejavu", "", 10)
    pdf.cell(0, 6, f"Datum: {_format_date(sale.created_at)}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # items table
    widths = (12, 66, 24, 24, 26, 26)
    headers = ("Pos.", "Artikel", "Menge", "€/kg", "Preis", "ID")
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
            format_amount(item),
            format_de(item.price_per_kg),
            f"{format_de(item.total_price)} €",
            item.uuid[:8].upper(),
        )
        for w, c, a in zip(widths, cells, aligns):
            pdf.cell(w, 7, c, border="B", align=a)
        pdf.ln()

    pdf.set_font("dejavu", "B", 11)
    pdf.cell(sum(widths[:4]), 9, "Gesamt", align="R")
    pdf.cell(widths[4], 9, f"{format_de(sale.total)} €", align="R")
    pdf.ln(16)

    pdf.set_font("dejavu", "", 9)
    pdf.cell(0, 5, "Wildbret aus eigener Jagd. Kein Ausweis der Umsatzsteuer (Kleinunternehmerregelung, § 19 UStG).", new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())

import io
import logging
import re
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from . import registry, sales
from .config import Hunter, PartDefaults, Preset, PresetItem, load_config, save_config
from .labels import render_info_label, render_qr_label
from .models import (
    PIECES_RE,
    PartIn,
    PartRecord,
    Sale,
    SaleItem,
    format_amount,
    format_de,
    parse_optional_decimal,
)
from .pdf import render_sale_pdf
from .printer import print_labels

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Gamecooler")
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
templates.env.filters["de"] = format_de
templates.env.filters["amount"] = format_amount


def print_safely(images: list, printer, dry_run: bool) -> str | None:
    """Print, returning an error message instead of raising.

    Records are saved before printing, so a printer that is off or a print
    agent that is unreachable must not fail the request — that would report an
    error for parts that were in fact stored. Callers surface the returned
    message and point at the reprint action in the inventory.
    """
    try:
        print_labels(images, printer, dry_run)
    except Exception as e:
        logger.exception("print failed on %s", printer.name)
        return str(e)
    return None


def parts_json(parts: dict[str, PartDefaults]) -> dict:
    return {name: p.model_dump() for name, p in parts.items()}


def presets_json(presets: list[Preset]) -> dict:
    return {p.name: [i.model_dump() for i in p.items] for p in presets}


def preset_text(preset: Preset) -> str:
    return ", ".join(f"{i.count}x {i.part}" for i in preset.items)


templates.env.filters["parts_json"] = parts_json
templates.env.filters["presets_json"] = presets_json
templates.env.filters["preset_text"] = preset_text

config = load_config()


@app.exception_handler(ValidationError)
def invalid_input(request: Request, exc: ValidationError):
    # Only reachable when the browser's pattern check is bypassed (old page,
    # typo on a device that ignores it). A message beats a bare 500.
    return RedirectResponse(
        url="/?msg=Ungültige Eingabe — Gewicht als 1,25 oder Stückzahl als 5x",
        status_code=303,
    )


def find_hunter(name: str) -> Hunter | None:
    return next((h for h in config.hunters if h.name == name), None)


def part_ingredients(part: str) -> str | None:
    defaults = config.parts.get(part)
    return defaults.ingredients if defaults else None


@app.get("/")
def index(request: Request, msg: str = ""):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"config": config, "msg": msg, "active": "index"},
    )


def _inventory_hint(records: list[PartRecord]) -> str:
    # Counted parts have no weight by design, so they never count as unweighed.
    unweighed = sum(1 for r in records if r.weight_kg is None and r.pieces is None)
    unpriced = sum(1 for r in records if (r.weight_kg is not None or r.pieces is not None) and r.total_price is None)
    notes = []
    if unweighed:
        notes.append(f"{unweighed} ohne Gewicht")
    if unpriced:
        notes.append(f"{unpriced} ohne Preis")
    return f" ({', '.join(notes)})" if notes else ""


@app.get("/inventory")
def inventory_page(request: Request, msg: str = ""):
    inventory = registry.inventory()
    return templates.TemplateResponse(
        request,
        "inventory.html",
        {
            "config": config,
            "msg": msg,
            "active": "inventory",
            "records": list(reversed(registry.load_all())),
            "inventory_count": len(inventory),
            "inventory_weight": sum(r.weight_kg for r in inventory if r.weight_kg is not None),
            "inventory_value": sum(r.total_price for r in inventory if r.total_price is not None),
            "inventory_hint": _inventory_hint(inventory),
        },
    )


@app.get("/preview")
def preview(
    hunter: str,
    species: str,
    part: str,
    weight_kg: str = "",
    price_per_kg: str = "",
    type: str = "info",
):
    part_in = PartIn(
        hunter=hunter, species=species, part=part,
        weight_kg=weight_kg,
        price_per_kg=price_per_kg,
        ingredients=part_ingredients(part),
    )
    record = PartRecord.from_input(part_in, printed=False)
    img = (
        render_info_label(record, find_hunter(hunter))
        if type == "info"
        else render_qr_label(record, config.best_before_months)
    )
    buf = io.BytesIO()
    img.convert("L").save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/bulk")
def bulk_form(request: Request, msg: str = ""):
    return templates.TemplateResponse(
        request,
        "bulk.html",
        {"config": config, "msg": msg, "active": "bulk"},
    )


@app.post("/bulk")
async def bulk_print(request: Request):
    form = await request.form()
    hunter = form["hunter"]
    species = form["species"]
    counts = form.getlist("count")
    parts = form.getlist("part")
    weights = form.getlist("weight_kg")
    prices = form.getlist("price_per_kg")
    no_prices = form.getlist("no_price")
    no_price_rows = {int(i) for i in no_prices}
    printer = config.printer(form.get("printer"))

    records: list[PartRecord] = []
    for i, (count, part, weight, price) in enumerate(zip(counts, parts, weights, prices)):
        if not part or not count:
            continue
        if i in no_price_rows:
            price = None
        elif PIECES_RE.match(weight):
            # Counted row: the price is a fixed price for the whole part. The
            # configured default is per kg, so it must not stand in for it.
            price = price.strip() or None
        else:
            price = price.strip() or getattr(config.parts.get(part), "price", None)
        part_in = PartIn(
            hunter=hunter, species=species, part=part,
            weight_kg=weight, price_per_kg=price,
            ingredients=part_ingredients(part),
        )
        for _ in range(int(count)):
            records.append(PartRecord.from_input(part_in, printed=False))

    if not records:
        return RedirectResponse(url="/bulk?msg=Keine gültigen Zeilen", status_code=303)

    hunter_config = find_hunter(hunter)
    images = []
    for record in records:
        images.append(render_info_label(record, hunter_config))
        images.append(render_qr_label(record, config.best_before_months))

    # Save first: printing can fail (printer off, agent host asleep) and must
    # not take the recorded parts down with it.
    registry.append_many(records)
    error = print_safely(images, printer, config.dry_run)
    if not error and not config.dry_run:
        registry.mark_printed([r.uuid for r in records])

    n = len(records)
    if error:
        msg = (
            f"{n} Teilstücke gespeichert, aber Druck fehlgeschlagen ({error}) "
            f"— über Bestand nachdrucken"
        )
    elif config.dry_run:
        msg = f"{n} Teilstücke gespeichert (Testmodus, nicht gedruckt)"
    else:
        msg = f"{n} Teilstücke ({2 * n} Etiketten) gedruckt & gespeichert"
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)


@app.post("/parts")
def create_part(
    hunter: str = Form(...),
    species: str = Form(...),
    part: str = Form(...),
    weight_kg: str = Form(""),
    price_per_kg: str = Form(""),
    printer: str = Form(""),
):
    part_in = PartIn(
        hunter=hunter, species=species, part=part,
        weight_kg=weight_kg, price_per_kg=price_per_kg,
        ingredients=part_ingredients(part),
    )
    record = PartRecord.from_input(part_in, printed=False)
    images = [render_info_label(record, find_hunter(hunter)), render_qr_label(record, config.best_before_months)]
    registry.append(record)
    error = print_safely(images, config.printer(printer), config.dry_run)
    if not error and not config.dry_run:
        registry.mark_printed([record.uuid])
    if error:
        msg = f"Gespeichert, aber Druck fehlgeschlagen ({error}) — über Bestand nachdrucken"
    elif config.dry_run:
        msg = "Gespeichert (Testmodus, nicht gedruckt)"
    else:
        msg = "Gedruckt & gespeichert"
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)


@app.post("/parts/{part_uuid}/reprint")
def reprint(part_uuid: str, printer: str = Form("")):
    record = registry.get(part_uuid)
    if record is None:
        raise HTTPException(status_code=404, detail="Teilstück nicht gefunden")
    images = [
        render_info_label(record, find_hunter(record.hunter)),
        render_qr_label(record, config.best_before_months),
    ]
    error = print_safely(images, config.printer(printer), config.dry_run)
    if error:
        msg = f"Nachdruck fehlgeschlagen ({error})"
    elif config.dry_run:
        msg = "Testmodus: nicht gedruckt"
    else:
        msg = "Erneut gedruckt"
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)


@app.get("/scan")
def scan_page(request: Request):
    return templates.TemplateResponse(
        request, "scan.html", {"config": config, "active": "scan"}
    )


@app.get("/parts/{part_uuid}.json")
def part_lookup(part_uuid: str):
    record = registry.get(part_uuid.strip().lower())
    if record is None:
        return JSONResponse(
            {"ok": False, "error": "Unbekannter Code — nicht im Bestand"},
            status_code=404,
        )
    return JSONResponse({"ok": True, "part": record.model_dump()})


@app.post("/parts/{part_uuid}/consume")
def consume(part_uuid: str):
    result = registry.mark_consumed(part_uuid.strip().lower())
    if result is None:
        return JSONResponse(
            {"ok": False, "error": "Unbekannter Code — nicht im Bestand"},
            status_code=404,
        )
    record, already_consumed = result
    return JSONResponse(
        {"ok": True, "already_consumed": already_consumed, "part": record.model_dump()}
    )


@app.post("/parts/consume-many")
async def consume_many(request: Request):
    form = await request.form()
    uuids = [u.strip().lower() for u in form.getlist("uuid")]
    if not uuids:
        return RedirectResponse(url="/inventory?msg=Nichts ausgewählt", status_code=303)
    changed = registry.mark_consumed_many(uuids)
    return RedirectResponse(
        url=f"/inventory?msg={changed} Teilstücke entnommen", status_code=303
    )


@app.post("/sell")
async def sell_form(request: Request):
    form = await request.form()
    uuids = [u.strip().lower() for u in form.getlist("uuid")]
    records = [r for r in registry.get_many(uuids) if r.consumed_at is None]
    if not records:
        return RedirectResponse(url="/inventory?msg=Nichts ausgewählt", status_code=303)
    return templates.TemplateResponse(
        request,
        "sell.html",
        {
            "config": config,
            "active": "inventory",
            "records": records,
            "total": sum(r.total_price for r in records if r.total_price is not None),
        },
    )


@app.post("/sell/confirm")
async def sell_confirm(request: Request):
    form = await request.form()
    uuids = form.getlist("uuid")
    prices = form.getlist("item_price")
    records = {r.uuid: r for r in registry.get_many(uuids) if r.consumed_at is None}
    items = []
    for u, price in zip(uuids, prices):
        record = records.get(u)
        if record is None:
            continue
        total = round(parse_optional_decimal(price) or 0.0, 2)
        items.append(
            SaleItem(
                uuid=record.uuid,
                species=record.species,
                part=record.part,
                weight_kg=record.weight_kg,
                pieces=record.pieces,
                price_per_kg=record.price_per_kg,
                total_price=total,
            )
        )
    if not items:
        return RedirectResponse(url="/inventory?msg=Keine Teilstücke im Verkauf", status_code=303)

    delivery = str(form.get("delivery_address", "")).strip()
    buyer_address = str(form.get("buyer_address", "")).strip()
    sale = Sale.create(
        number=sales.next_number(),
        hunter=str(form.get("hunter", "")),
        buyer_name=str(form.get("buyer_name", "")).strip(),
        buyer_address=buyer_address,
        delivery_address=delivery or buyer_address,
        items=items,
    )
    sales.append(sale)
    registry.mark_sold([i.uuid for i in items], sale.sale_id)
    return RedirectResponse(url=f"/sales/{sale.sale_id}", status_code=303)


@app.get("/sales/{sale_id}")
def sale_page(request: Request, sale_id: str):
    sale = sales.get(sale_id)
    if sale is None:
        raise HTTPException(status_code=404, detail="Verkauf nicht gefunden")
    return templates.TemplateResponse(
        request,
        "sale.html",
        {"config": config, "active": "inventory", "sale": sale},
    )


@app.get("/sales/{sale_id}/pdf")
def sale_pdf(sale_id: str):
    sale = sales.get(sale_id)
    if sale is None:
        raise HTTPException(status_code=404, detail="Verkauf nicht gefunden")
    pdf_bytes = render_sale_pdf(sale, find_hunter(sale.hunter))
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="verkauf-{sale.number}.pdf"'},
    )


def parse_preset_items(text: str) -> list[PresetItem] | None:
    items: list[PresetItem] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^(\d+)\s*x\s*(.+)$", chunk)
        if not m:
            return None
        items.append(PresetItem(part=m.group(2).strip(), count=int(m.group(1))))
    return items or None


@app.post("/settings/presets")
def settings_preset_save(
    original_name: str = Form(""),
    name: str = Form(...),
    items_text: str = Form(...),
):
    items = parse_preset_items(items_text)
    if items is None:
        return RedirectResponse(url="/settings?msg=Ungültiges Format — erwartet z. B. „2x Keule, 1x Rücken“", status_code=303)
    preset = Preset(name=name.strip(), items=items)
    config.presets = [p for p in config.presets if p.name != original_name]
    config.presets.append(preset)
    return _settings_redirect(f"Vorgabe „{preset.name}“ gespeichert")


@app.post("/settings/presets/delete")
def settings_preset_delete(name: str = Form(...)):
    config.presets = [p for p in config.presets if p.name != name]
    return _settings_redirect(f"Vorgabe „{name}“ gelöscht")


@app.get("/settings")
def settings_page(request: Request, msg: str = ""):
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"config": config, "msg": msg, "active": "settings"},
    )


def _settings_redirect(msg: str) -> RedirectResponse:
    save_config(config)
    return RedirectResponse(url=f"/settings?msg={msg}", status_code=303)


@app.post("/settings/hunters")
def settings_hunter_save(
    original_name: str = Form(""),
    name: str = Form(...),
    address: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
):
    hunter = Hunter(name=name.strip(), address=address.strip(), phone=phone.strip(), email=email.strip())
    existing = find_hunter(original_name)
    if existing:
        config.hunters[config.hunters.index(existing)] = hunter
    else:
        config.hunters.append(hunter)
    return _settings_redirect(f"Jäger „{hunter.name}“ gespeichert")


@app.post("/settings/hunters/delete")
def settings_hunter_delete(name: str = Form(""), original_name: str = Form("")):
    name = original_name or name
    hunter = find_hunter(name)
    if hunter is None:
        raise HTTPException(status_code=404, detail="Jäger nicht gefunden")
    if len(config.hunters) == 1:
        return RedirectResponse(url="/settings?msg=Der letzte Jäger kann nicht gelöscht werden", status_code=303)
    config.hunters.remove(hunter)
    return _settings_redirect(f"Jäger „{name}“ gelöscht")


@app.post("/settings/species")
def settings_species_add(name: str = Form(...)):
    name = name.strip()
    if name and name not in config.species:
        config.species.append(name)
        return _settings_redirect(f"Wildart „{name}“ hinzugefügt")
    return RedirectResponse(url="/settings?msg=Wildart existiert bereits", status_code=303)


@app.post("/settings/species/delete")
def settings_species_delete(name: str = Form(...)):
    if name not in config.species:
        raise HTTPException(status_code=404, detail="Wildart nicht gefunden")
    config.species.remove(name)
    return _settings_redirect(f"Wildart „{name}“ gelöscht")


@app.post("/settings/parts")
def settings_part_save(
    original_name: str = Form(""),
    name: str = Form(...),
    price: str = Form(""),
    weight_kg: str = Form(""),
    ingredients: str = Form(""),
):
    name = name.strip()
    defaults = PartDefaults(
        price=parse_optional_decimal(price),
        weight_kg=parse_optional_decimal(weight_kg),
        ingredients=ingredients,
    )
    if original_name and original_name != name:
        config.parts.pop(original_name, None)
    config.parts[name] = defaults
    return _settings_redirect(f"Teilstück „{name}“ gespeichert")


@app.post("/settings/parts/delete")
def settings_part_delete(name: str = Form(""), original_name: str = Form("")):
    name = original_name or name
    if name not in config.parts:
        raise HTTPException(status_code=404, detail="Teilstück nicht gefunden")
    del config.parts[name]
    return _settings_redirect(f"Teilstück „{name}“ gelöscht")


@app.get("/parts.json")
def export():
    return JSONResponse([r.model_dump() for r in registry.load_all()])

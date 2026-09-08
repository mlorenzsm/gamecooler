import io
import logging
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import registry
from .config import Hunter, load_config
from .labels import render_info_label, render_qr_label
from .models import PartIn, PartRecord, format_de
from .printer import print_labels

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Wildbret-Etiketten")
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
templates.env.filters["de"] = format_de

config = load_config()


def find_hunter(name: str) -> Hunter | None:
    return next((h for h in config.hunters if h.name == name), None)


@app.get("/")
def index(request: Request, msg: str = ""):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"config": config, "msg": msg, "active": "index"},
    )


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
            "inventory_weight": sum(r.weight_kg for r in inventory),
            "inventory_value": sum(r.total_price for r in inventory),
        },
    )


@app.get("/preview")
def preview(
    hunter: str,
    species: str,
    part: str,
    weight_kg: str,
    price_per_kg: str,
    type: str = "info",
):
    part_in = PartIn(
        hunter=hunter, species=species, part=part,
        weight_kg=weight_kg, price_per_kg=price_per_kg,
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

    records: list[PartRecord] = []
    for count, part, weight, price in zip(counts, parts, weights, prices):
        if not part or not weight:
            continue
        price = price.strip() or format_de(config.parts[part])
        part_in = PartIn(
            hunter=hunter, species=species, part=part,
            weight_kg=weight, price_per_kg=price,
        )
        for _ in range(int(count)):
            records.append(PartRecord.from_input(part_in, printed=not config.dry_run))

    if not records:
        return RedirectResponse(url="/bulk?msg=Keine gültigen Zeilen", status_code=303)

    hunter_config = find_hunter(hunter)
    images = []
    for record in records:
        images.append(render_info_label(record, hunter_config))
        images.append(render_qr_label(record, config.best_before_months))
    print_labels(images, config.printer, config.dry_run)
    registry.append_many(records)

    n = len(records)
    msg = (
        f"{n} Teilstücke ({2 * n} Etiketten) gedruckt & gespeichert"
        if not config.dry_run
        else f"{n} Teilstücke gespeichert (Testmodus, nicht gedruckt)"
    )
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)


@app.post("/parts")
def create_part(
    hunter: str = Form(...),
    species: str = Form(...),
    part: str = Form(...),
    weight_kg: str = Form(...),
    price_per_kg: str = Form(...),
):
    part_in = PartIn(
        hunter=hunter, species=species, part=part,
        weight_kg=weight_kg, price_per_kg=price_per_kg,
    )
    record = PartRecord.from_input(part_in, printed=not config.dry_run)
    images = [render_info_label(record, find_hunter(hunter)), render_qr_label(record, config.best_before_months)]
    print_labels(images, config.printer, config.dry_run)
    registry.append(record)
    msg = "Gedruckt & gespeichert" if not config.dry_run else "Gespeichert (Testmodus, nicht gedruckt)"
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)


@app.post("/parts/{part_uuid}/reprint")
def reprint(part_uuid: str):
    record = registry.get(part_uuid)
    if record is None:
        raise HTTPException(status_code=404, detail="Teilstück nicht gefunden")
    images = [
        render_info_label(record, find_hunter(record.hunter)),
        render_qr_label(record, config.best_before_months),
    ]
    print_labels(images, config.printer, config.dry_run)
    msg = "Erneut gedruckt" if not config.dry_run else "Testmodus: nicht gedruckt"
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)


@app.get("/scan")
def scan_page(request: Request):
    return templates.TemplateResponse(
        request, "scan.html", {"config": config, "active": "scan"}
    )


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


@app.get("/parts.json")
def export():
    return JSONResponse([r.model_dump() for r in registry.load_all()])

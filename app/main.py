import hashlib
import io
import logging
import re
from datetime import date
from urllib.parse import quote
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from pydantic import ValidationError

from . import paperless, registry, sales
from .config import PART_KINDS, UNITS, Hunter, PartDefaults, Preset, PresetItem, Recipe, RecipeItem, Species, load_config, save_config
from .i18n import LANGUAGES, decimal_separator, format_date, get_lang, pick_language, reset_lang, set_lang, t
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

# Appended to static asset URLs (?v=…) so a deploy never leaves a phone on the
# old stylesheet: a hash of the content, so it changes exactly when they do.
_STATIC = Path(__file__).resolve().parent / "static"
ASSET_VERSION = hashlib.sha256(
    b"".join((_STATIC / f).read_bytes() for f in ("app.css", "icons.svg"))
).hexdigest()[:10]
templates.env.globals["asset_version"] = ASSET_VERSION
templates.env.filters["de"] = format_de
templates.env.filters["amount"] = format_amount


def _msg_url(path: str, msg: str, error: object = None) -> str:
    """Redirect target with a message; an error shows red instead of green."""
    return f"{path}?{'kind=error&' if error else ''}msg={quote(msg)}"


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


def date_filter(iso: str | None) -> str:
    """ISO timestamp -> date in the UI language: 23.09.2026 / 23 Sep 2026."""
    return format_date(date.fromisoformat(iso[:10])) if iso else "–"


def species_json(species: list[Species]) -> dict:
    """Parts and presets per species, for the entry pages' JavaScript."""
    return {
        s.name: {
            # Teilstücke first, then Zubereitungen, so the dropdowns can group
            # them without re-sorting.
            "parts": {
                name: p.model_dump()
                for group in s.parts_by_kind().values()
                for name, p in group.items()
            },
            "presets": {pr.name: [i.model_dump() for i in pr.items] for pr in s.presets},
        }
        for s in species
    }


def all_part_names(species: list[Species]) -> dict[str, list[str]]:
    """Every part name across all species, grouped by kind, for filters.

    A name that is a Teilstück for one species and a Zubereitung for another
    is listed under both headings — the filter matches by name either way.
    """
    groups: dict[str, dict[str, None]] = {kind: {} for kind in PART_KINDS}
    for s in species:
        for name, p in s.parts.items():
            groups[p.kind].setdefault(name)
    return {kind: list(names) for kind, names in groups.items()}


def preset_text(preset: Preset) -> str:
    return ", ".join(f"{i.count}x {i.part}" for i in preset.items)


templates.env.filters["species_json"] = species_json
templates.env.filters["all_part_names"] = all_part_names
templates.env.globals["PART_KINDS"] = PART_KINDS
# Translation in templates: {{ t("Bestand") }}. The UI language is set per
# request by the middleware below.
templates.env.globals["t"] = t
templates.env.globals["get_lang"] = get_lang
templates.env.globals["LANGUAGES"] = LANGUAGES
templates.env.globals["decimal_separator"] = decimal_separator


def icon(name: str, label: str = "") -> Markup:
    """Inline reference to a Lucide icon in static/icons.svg.

    Decorative by default (aria-hidden); pass a label when the icon stands
    alone and carries meaning.
    """
    attrs = f'role="img" aria-label="{escape(label)}"' if label else 'aria-hidden="true"'
    return Markup(f'<svg class="icon" {attrs}><use href="/static/icons.svg?v={ASSET_VERSION}#{escape(name)}"/></svg>')


templates.env.globals["icon"] = icon
templates.env.filters["date"] = date_filter
# {"cut": "Teilstücke", ...} with the headings in the UI language
templates.env.filters["map_kinds"] = lambda kinds: {k: t(v) for k, v in kinds.items()}
# tojson sorts object keys by default, which would throw away the part order
# set by drag and drop (and presets' order) on the way to the page.
templates.env.policies["json.dumps_kwargs"] = {"sort_keys": False}
templates.env.filters["preset_text"] = preset_text

config = load_config()

LANG_COOKIE = "lang"


@app.middleware("http")
async def ui_language(request: Request, call_next):
    """Pick the UI language: the cookie from the DE/EN switch, else the
    browser's Accept-Language, else German."""
    lang = request.cookies.get(LANG_COOKIE)
    if lang not in LANGUAGES:
        lang = pick_language(request.headers.get("accept-language"))
    token = set_lang(lang)
    try:
        return await call_next(request)
    finally:
        reset_lang(token)


@app.get("/lang/{lang}")
def switch_language(lang: str, request: Request, back: str = "/"):
    """Remember the chosen language and go back to the page it was chosen on."""
    # only same-site paths, so the link can't be turned into an open redirect
    target = back if back.startswith("/") and not back.startswith("//") else "/"
    response = RedirectResponse(url=target, status_code=303)
    if lang in LANGUAGES:
        response.set_cookie(LANG_COOKIE, lang, max_age=365 * 24 * 3600, samesite="lax")
    return response


@app.exception_handler(ValidationError)
def invalid_input(request: Request, exc: ValidationError):
    # Reachable when the browser's pattern check is bypassed (old page, typo
    # on a device that ignores it). A message beats a bare 500 — but only for
    # the entry form's input; any other validation error is a bug and must
    # stay visible instead of being disguised as a typo.
    if exc.title != "PartIn":
        raise exc
    return RedirectResponse(
        url="/?kind=error&msg=" + quote(t("Ungültige Eingabe — Gewicht als 1,25 oder Stückzahl als 5x")),
        status_code=303,
    )


def find_hunter(name: str) -> Hunter | None:
    return next((h for h in config.hunters if h.name == name), None)


def part_ingredients(species: str, part: str) -> str | None:
    # A linked recipe wins over typed text, and is read at print time — so a
    # recipe change reaches the next label. The record still keeps a copy, so
    # reprints of older parts show the recipe as it was then.
    return config.ingredients_for(species, part)


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
        notes.append(t("{n} ohne Gewicht", n=unweighed))
    if unpriced:
        notes.append(t("{n} ohne Preis", n=unpriced))
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
        ingredients=part_ingredients(species, part),
    )
    record = PartRecord.from_input(part_in, printed=False)
    img = (
        render_info_label(record, find_hunter(hunter), config.label_language)
        if type == "info"
        else render_qr_label(record, config.best_before_months, config.label_language)
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
            price = price.strip() or getattr(config.part(species, part), "price", None)
        part_in = PartIn(
            hunter=hunter, species=species, part=part,
            weight_kg=weight, price_per_kg=price,
            ingredients=part_ingredients(species, part),
        )
        for _ in range(int(count)):
            records.append(PartRecord.from_input(part_in, printed=False))

    if not records:
        return RedirectResponse(url="/bulk?kind=error&msg=" + quote(t("Keine gültigen Zeilen")), status_code=303)

    hunter_config = find_hunter(hunter)
    images = []
    for record in records:
        images.append(render_info_label(record, hunter_config, config.label_language))
        images.append(render_qr_label(record, config.best_before_months, config.label_language))

    # Save first: printing can fail (printer off, agent host asleep) and must
    # not take the recorded parts down with it.
    registry.append_many(records)
    error = print_safely(images, printer, config.dry_run)
    if not error and not config.dry_run:
        registry.mark_printed([r.uuid for r in records])

    n = len(records)
    if error:
        msg = t("{n} Teilstücke gespeichert, aber Druck fehlgeschlagen ({error}) — über Bestand nachdrucken",
                n=n, error=error)
    elif config.dry_run:
        msg = t("{n} Teilstücke gespeichert (Testmodus, nicht gedruckt)", n=n)
    else:
        msg = t("{n} Teilstücke ({labels} Etiketten) gedruckt & gespeichert", n=n, labels=2 * n)
    return RedirectResponse(url=_msg_url("/", msg, error), status_code=303)


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
        ingredients=part_ingredients(species, part),
    )
    record = PartRecord.from_input(part_in, printed=False)
    images = [
        render_info_label(record, find_hunter(hunter), config.label_language),
        render_qr_label(record, config.best_before_months, config.label_language),
    ]
    registry.append(record)
    error = print_safely(images, config.printer(printer), config.dry_run)
    if not error and not config.dry_run:
        registry.mark_printed([record.uuid])
    if error:
        msg = t("Gespeichert, aber Druck fehlgeschlagen ({error}) — über Bestand nachdrucken", error=error)
    elif config.dry_run:
        msg = t("Gespeichert (Testmodus, nicht gedruckt)")
    else:
        msg = t("Gedruckt & gespeichert")
    return RedirectResponse(url=_msg_url("/", msg, error), status_code=303)


def _amount_text(record: PartRecord) -> str:
    """The amount as typed into the Stock row: "1,250", "5x" or empty."""
    if record.pieces is not None:
        return f"{record.pieces}x"
    return format_de(record.weight_kg, 3) if record.weight_kg is not None else ""


def _price_text(record: PartRecord) -> str:
    """The price as typed: per kg for weighed parts, fixed for counted ones."""
    price = record.total_price if record.pieces is not None else record.price_per_kg
    return format_de(price, 2) if price is not None else ""


def _row_json(record: PartRecord) -> dict:
    """What the Stock row shows after an edit, formatted for the UI language."""
    return {
        "ok": True,
        "amount": format_amount(record),
        "amount_input": _amount_text(record),
        "price_input": _price_text(record),
        "price_per_kg": format_de(record.price_per_kg),
        "total_price": format_de(record.total_price),
        "weight_kg": record.weight_kg or 0,
        "total": record.total_price or 0,
        "pieces": record.pieces is not None,
        "unweighed": record.weight_kg is None and record.pieces is None,
        "unpriced": record.total_price is None,
        "reprint_question": t("2 Etiketten nachdrucken?") + "\n\n"
            + f"{record.species} – {record.part}, {format_amount(record)}",
    }


templates.env.globals["amount_text"] = _amount_text
templates.env.globals["price_text"] = _price_text


@app.post("/parts/{part_uuid}/edit")
def edit_part(part_uuid: str, weight_kg: str = Form(""), price_per_kg: str = Form("")):
    """Correct amount and price of an entry, from the Stock row. Everything
    else stays as entered; the uuid and date too, so the label on the package
    still scans. Reprinting afterwards prints labels for the same uuid."""
    record = registry.get(part_uuid)
    if record is None:
        return JSONResponse({"ok": False, "error": t("Teilstück nicht gefunden")}, status_code=404)
    if record.consumed_at is not None:
        return JSONResponse({"ok": False, "error": t("Bereits entnommen — nicht mehr änderbar")}, status_code=409)
    try:
        part_in = PartIn(
            hunter=record.hunter, species=record.species, part=record.part,
            weight_kg=weight_kg, price_per_kg=price_per_kg,
            ingredients=record.ingredients,
        )
    except (ValidationError, ValueError):
        return JSONResponse(
            {"ok": False, "error": t("Ungültige Eingabe — Gewicht als 1,25 oder Stückzahl als 5x")}, status_code=400
        )
    record = record.edited(part_in)
    registry.update(record)
    return JSONResponse(_row_json(record))


@app.post("/parts/{part_uuid}/reprint")
def reprint(part_uuid: str, printer: str = Form("")):
    record = registry.get(part_uuid)
    if record is None:
        raise HTTPException(status_code=404, detail=t("Teilstück nicht gefunden"))
    images = [
        render_info_label(record, find_hunter(record.hunter), config.label_language),
        render_qr_label(record, config.best_before_months, config.label_language),
    ]
    error = print_safely(images, config.printer(printer), config.dry_run)
    if error:
        msg = t("Nachdruck fehlgeschlagen ({error})", error=error)
    elif config.dry_run:
        msg = t("Testmodus: nicht gedruckt")
    else:
        msg = t("Erneut gedruckt")
    return RedirectResponse(url=_msg_url("/", msg, error), status_code=303)


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
            {"ok": False, "error": t("Unbekannter Code — nicht im Bestand")},
            status_code=404,
        )
    return JSONResponse({"ok": True, "part": record.model_dump()})


@app.post("/parts/{part_uuid}/consume")
def consume(part_uuid: str):
    result = registry.mark_consumed(part_uuid.strip().lower())
    if result is None:
        return JSONResponse(
            {"ok": False, "error": t("Unbekannter Code — nicht im Bestand")},
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
        return RedirectResponse(url="/inventory?msg=" + quote(t("Nichts ausgewählt")), status_code=303)
    changed = registry.mark_consumed_many(uuids)
    return RedirectResponse(
        url="/inventory?msg=" + quote(t("{n} Teilstücke entnommen", n=changed)), status_code=303
    )


@app.post("/sell")
async def sell_form(request: Request):
    form = await request.form()
    uuids = [u.strip().lower() for u in form.getlist("uuid")]
    records = [r for r in registry.get_many(uuids) if r.consumed_at is None]
    if not records:
        return RedirectResponse(url="/inventory?msg=" + quote(t("Nichts ausgewählt")), status_code=303)
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


def _schedule_paperless(background: BackgroundTasks, sale: Sale) -> None:
    """Upload the invoice after the response is sent — a slow or unreachable
    Paperless must not hold up (or fail) completing the sale."""
    if paperless.enabled():
        background.add_task(paperless.upload_sale, sale.sale_id, find_hunter(sale.hunter),
                            config.paperless, config.label_language)


@app.post("/sell/confirm")
async def sell_confirm(request: Request, background: BackgroundTasks):
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
        return RedirectResponse(url="/inventory?msg=" + quote(t("Keine Teilstücke im Verkauf")), status_code=303)

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
    _schedule_paperless(background, sale)
    return RedirectResponse(url=f"/sales/{sale.sale_id}", status_code=303)


@app.get("/sales/{sale_id}")
def sale_page(request: Request, sale_id: str, msg: str = ""):
    sale = sales.get(sale_id)
    if sale is None:
        raise HTTPException(status_code=404, detail=t("Verkauf nicht gefunden"))
    # a still-pending upload may have finished in the meantime
    sale = paperless.refresh(sale)
    return templates.TemplateResponse(
        request,
        "sale.html",
        {"config": config, "active": "inventory", "sale": sale, "msg": msg,
         "paperless_on": paperless.enabled(),
         "paperless_link": paperless.document_url(sale.paperless.document_id)
             if sale.paperless and sale.paperless.document_id else ""},
    )


@app.post("/sales/{sale_id}/paperless")
def sale_paperless(sale_id: str, background: BackgroundTasks):
    """Send (or resend) a sale's invoice. Safe to press twice: upload_sale
    checks an earlier task before uploading again."""
    sale = sales.get(sale_id)
    if sale is None:
        raise HTTPException(status_code=404, detail=t("Verkauf nicht gefunden"))
    if not paperless.enabled():
        return RedirectResponse(_msg_url(f"/sales/{sale_id}", t("Paperless ist nicht eingerichtet"), error=True), status_code=303)
    _schedule_paperless(background, sale)
    return RedirectResponse(_msg_url(f"/sales/{sale_id}", t("Wird an Paperless gesendet …")), status_code=303)


@app.get("/sales/{sale_id}/pdf")
def sale_pdf(sale_id: str):
    sale = sales.get(sale_id)
    if sale is None:
        raise HTTPException(status_code=404, detail=t("Verkauf nicht gefunden"))
    pdf_bytes = render_sale_pdf(sale, find_hunter(sale.hunter), config.label_language)
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


def _species_or_404(name: str) -> Species:
    species = config.find_species(name)
    if species is None:
        raise HTTPException(status_code=404, detail=t("Wildart nicht gefunden"))
    return species


@app.post("/settings/presets")
def settings_preset_save(
    species: str = Form(...),
    original_name: str = Form(""),
    name: str = Form(...),
    items_text: str = Form(...),
):
    sp = _species_or_404(species)
    items = parse_preset_items(items_text)
    if items is None:
        return _settings_error(sp.name, t("Ungültiges Format — erwartet z. B. „2x Keule, 1x Rücken“"))
    # A preset may only use parts this species has; otherwise bulk print would
    # fill rows with a part that can't be selected for it.
    unknown = [i.part for i in items if i.part not in sp.parts]
    if unknown:
        return _settings_error(sp.name, t("Unbekannte Teilstücke für {species}: {parts}", species=sp.name, parts=", ".join(unknown)))
    preset = Preset(name=name.strip(), items=items)
    sp.presets = [p for p in sp.presets if p.name != original_name]
    sp.presets.append(preset)
    return _settings_redirect(t("Vorgabe „{name}“ für {species} gespeichert", name=preset.name, species=sp.name), sp.name)


@app.post("/settings/presets/delete")
def settings_preset_delete(species: str = Form(...), name: str = Form(...)):
    sp = _species_or_404(species)
    sp.presets = [p for p in sp.presets if p.name != name]
    return _settings_redirect(t("Vorgabe „{name}“ für {species} gelöscht", name=name, species=sp.name), sp.name)


@app.get("/settings")
def settings_page(request: Request, msg: str = "", species: str = "", tab: str = ""):
    # Two tabs: "general" (hunters, label language) and "species" (one species
    # at a time). Naming a species implies the species tab.
    current = config.find_species(species)
    if tab not in ("general", "species"):
        tab = "species" if current else "general"
    if tab == "species" and current is None:
        current = config.species[0]
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "config": config, "msg": msg, "active": "settings", "tab": tab, "sp": current,
            # per species, since recipes are: {species: {recipe: label text}}
            "recipe_texts": {
                sp.name: {r.name: r.label_text() or "" for r in sp.recipes} for sp in config.species
            },
            "paperless_on": paperless.enabled(),
            "paperless_url": paperless.base_url(),
        },
    )


@app.post("/settings/paperless/test")
def settings_paperless_test():
    ok, detail = paperless.check_connection()
    if ok:
        msg = t("Verbindung zu Paperless steht, die Rechte reichen.")
    else:
        msg = t("Paperless nicht erreichbar: {error}", error=detail)
    return RedirectResponse(_msg_url("/settings", msg, error=not ok) + "&tab=general", status_code=303)


def _settings_url(msg: str, species: str = "") -> str:
    # Back to the species that was just edited; without one, to the general tab.
    url = f"/settings?msg={quote(msg)}"
    if config.find_species(species):
        url += f"&species={quote(species)}"
    return url


def _settings_redirect(msg: str, species: str = "") -> RedirectResponse:
    save_config(config)
    return RedirectResponse(url=_settings_url(msg, species), status_code=303)


def _settings_general(msg: str) -> RedirectResponse:
    save_config(config)
    return RedirectResponse(url=f"/settings?tab=general&msg={quote(msg)}", status_code=303)


def _settings_error(species: str, msg: str) -> RedirectResponse:
    return RedirectResponse(url=_settings_url(msg, species), status_code=303)


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
    return _settings_general(t("Jäger „{name}“ gespeichert", name=hunter.name))


@app.post("/settings/hunters/delete")
def settings_hunter_delete(name: str = Form(""), original_name: str = Form("")):
    name = original_name or name
    hunter = find_hunter(name)
    if hunter is None:
        raise HTTPException(status_code=404, detail=t("Jäger nicht gefunden"))
    if len(config.hunters) == 1:
        return RedirectResponse(url="/settings?tab=general&msg=" + quote(t("Der letzte Jäger kann nicht gelöscht werden")), status_code=303)
    config.hunters.remove(hunter)
    return _settings_general(t("Jäger „{name}“ gelöscht", name=name))


@app.post("/settings/label-language")
def settings_label_language(label_language: str = Form(...)):
    if label_language in LANGUAGES:
        config.label_language = label_language
    return _settings_general(t("Etikettensprache: {language}", language=LANGUAGES[config.label_language]))


@app.post("/settings/species")
def settings_species_add(name: str = Form(...), copy_from: str = Form("")):
    name = name.strip()
    if not name or config.find_species(name):
        return RedirectResponse(url="/settings?msg=" + quote(t("Wildart existiert bereits")), status_code=303)
    # Optionally start from another species' parts, so a new deer species
    # doesn't need every cut typed in again.
    source = config.find_species(copy_from)
    config.species.append(Species(
        name=name,
        parts={n: p.model_copy() for n, p in source.parts.items()} if source else {},
    ))
    return _settings_redirect(t("Wildart „{name}“ hinzugefügt", name=name), name)


@app.post("/settings/species/rename")
def settings_species_rename(original_name: str = Form(...), name: str = Form(...)):
    sp = _species_or_404(original_name)
    name = name.strip()
    if not name or name == sp.name:
        return _settings_error(sp.name, t("Name fehlt") if not name else t("Unverändert"))
    if config.find_species(name):
        return _settings_error(sp.name, t("Wildart existiert bereits"))
    # Existing records keep the old name — they describe what was printed.
    sp.name = name
    return _settings_redirect(t("Wildart „{old}“ heißt jetzt „{name}“", old=original_name, name=name), name)


@app.post("/settings/species/delete")
def settings_species_delete(name: str = Form(...)):
    sp = _species_or_404(name)
    if len(config.species) == 1:
        return RedirectResponse(url="/settings?msg=" + quote(t("Die letzte Wildart kann nicht gelöscht werden")), status_code=303)
    config.species.remove(sp)
    return _settings_redirect(t("Wildart „{name}“ gelöscht", name=name), config.species[0].name)


@app.post("/settings/parts")
def settings_part_save(
    species: str = Form(...),
    original_name: str = Form(""),
    name: str = Form(...),
    price: str = Form(""),
    weight_kg: str = Form(""),
    ingredients: str = Form(""),
    kind: str = Form("cut"),
    recipe: str = Form(""),
):
    sp = _species_or_404(species)
    name = name.strip()
    if name != original_name and name in sp.parts:
        return _settings_error(sp.name, t("„{name}“ gibt es bei {species} schon", name=name, species=sp.name))
    linked = sp.find_recipe(recipe)
    previous = sp.parts.get(original_name or name)
    defaults = PartDefaults(
        price=parse_optional_decimal(price),
        weight_kg=parse_optional_decimal(weight_kg),
        # With a recipe linked the field only shows the recipe's text; keep the
        # part's own text as it was, so unlinking later brings it back.
        ingredients=(previous.ingredients if previous else None) if linked else ingredients,
        recipe=linked.name if linked else None,
    )
    if original_name and original_name != name:
        # Rename in place so the part keeps its position, and carry the new
        # name into this species' presets so they don't point at a ghost.
        sp.parts = {(name if k == original_name else k): v for k, v in sp.parts.items()}
        for preset in sp.presets:
            for item in preset.items:
                if item.part == original_name:
                    item.part = name
    # Editing a row must not move it to the other list: the kind is only
    # changed by dragging. A new part lands in the list it was added to.
    existing = sp.parts.get(name)
    defaults.kind = existing.kind if existing else (kind if kind in PART_KINDS else "cut")
    if defaults.kind != "prep":
        # Only Zubereitungen have recipes; a Teilstück row doesn't show the choice.
        defaults.recipe = None
        defaults.ingredients = ingredients or None
    sp.parts[name] = defaults
    return _settings_redirect(t("„{name}“ ({species}) gespeichert", name=name, species=sp.name), sp.name)


@app.post("/settings/parts/order")
async def settings_part_order(request: Request):
    """Apply a drag-and-drop arrangement of one species' parts.

    The page sends the complete new layout — every part with its kind, in
    display order — so a drop can't leave the two lists half-updated. It must
    name exactly the parts the species has; anything else means the page is
    stale (edited in another tab) and is refused rather than guessed at.
    """
    body = await request.json()
    sp = _species_or_404(str(body.get("species", "")))
    layout = body.get("parts") or []
    names = [str(e.get("name", "")) for e in layout]
    if sorted(names) != sorted(sp.parts) or len(set(names)) != len(names):
        return JSONResponse(
            {"ok": False, "error": t("Seite ist veraltet — bitte neu laden")}, status_code=409
        )
    reordered = {}
    for entry, name in zip(layout, names):
        part = sp.parts[name]
        part.kind = entry.get("kind") if entry.get("kind") in PART_KINDS else "cut"
        if part.kind != "prep":
            # Moved to Teilstücke: those have no recipe, so the link goes. The
            # part's own ingredient text stays and is used from now on.
            part.recipe = None
        reordered[name] = part
    sp.parts = reordered
    save_config(config)
    return JSONResponse({"ok": True})


@app.post("/settings/parts/delete")
def settings_part_delete(species: str = Form(...), name: str = Form(""), original_name: str = Form("")):
    sp = _species_or_404(species)
    name = original_name or name
    if name not in sp.parts:
        raise HTTPException(status_code=404, detail=t("Teilstück nicht gefunden"))
    in_presets = [p.name for p in sp.presets if any(i.part == name for i in p.items)]
    if in_presets:
        return _settings_error(sp.name, t("„{name}“ wird noch in Vorgaben verwendet: {presets}", name=name, presets=", ".join(in_presets)))
    del sp.parts[name]
    return _settings_redirect(t("„{name}“ ({species}) gelöscht", name=name, species=sp.name), sp.name)


# --- Rezepte ----------------------------------------------------------------
#
# Recipes belong to a species, like parts: every route names the species, and
# a part can only link a recipe of its own species.


def _recipes_url(msg: str, species: str = "", name: str = "") -> str:
    url = f"/recipes?msg={quote(msg)}"
    if species:
        url += f"&species={quote(species)}"
    if name:
        url += f"&open={quote(name)}"
    return url


def _recipes_redirect(msg: str, species: str = "", name: str = "") -> RedirectResponse:
    save_config(config)
    return RedirectResponse(url=_recipes_url(msg, species, name), status_code=303)


@app.get("/recipes")
def recipes_page(request: Request, msg: str = "", species: str = "", open: str = ""):
    # One species at a time; default to the first one that has recipes, so the
    # page doesn't open on an empty list when there is something to show.
    current = config.find_species(species) or next(
        (s for s in config.species if s.recipes), config.species[0]
    )
    return templates.TemplateResponse(
        request,
        "recipes.html",
        {
            "config": config, "msg": msg, "active": "recipes",
            "sp": current, "open_recipe": open, "units": list(UNITS),
        },
    )


@app.post("/recipes")
async def recipe_save(request: Request):
    """Create or update a recipe of one species. Ingredient lines arrive as parallel lists."""
    form = await request.form()
    sp = _species_or_404(str(form.get("species", "")))
    original_name = str(form.get("original_name", ""))
    name = str(form.get("name", "")).strip()
    if not name:
        return RedirectResponse(url=_recipes_url(t("Name fehlt"), sp.name), status_code=303)
    if name != original_name and sp.find_recipe(name):
        return RedirectResponse(url=_recipes_url(t("Rezept „{name}“ gibt es bei {species} schon", name=name, species=sp.name), sp.name), status_code=303)
    items = [
        RecipeItem(name=n.strip(), amount=a, unit=u)
        for n, a, u in zip(form.getlist("item_name"), form.getlist("item_amount"), form.getlist("item_unit"))
        if n.strip()
    ]
    recipe = Recipe(name=name, items=items, notes=str(form.get("notes", "")).strip())
    existing = sp.find_recipe(original_name)
    if existing:
        sp.recipes[sp.recipes.index(existing)] = recipe
        if name != original_name:
            # carry the rename into this species' linked parts, or they'd lose the link
            for part in sp.parts.values():
                if part.recipe == original_name:
                    part.recipe = name
    else:
        sp.recipes.append(recipe)
    return _recipes_redirect(t("Rezept „{name}“ ({species}) gespeichert", name=name, species=sp.name), sp.name, name)


@app.post("/recipes/copy")
def recipe_copy(species: str = Form(...), original_name: str = Form(...), target: str = Form(...)):
    """Copy a recipe to another species, as the starting point for its own version."""
    sp = _species_or_404(species)
    recipe = sp.find_recipe(original_name)
    if recipe is None:
        raise HTTPException(status_code=404, detail=t("Rezept nicht gefunden"))
    dest = _species_or_404(target)
    if dest.find_recipe(recipe.name):
        return RedirectResponse(
            url=_recipes_url(t("Rezept „{name}“ gibt es bei {species} schon", name=recipe.name, species=dest.name), sp.name, recipe.name),
            status_code=303,
        )
    dest.recipes.append(recipe.model_copy(deep=True))
    return _recipes_redirect(t("Rezept „{name}“ nach {species} kopiert", name=recipe.name, species=dest.name), dest.name, recipe.name)


@app.post("/recipes/delete")
def recipe_delete(species: str = Form(...), original_name: str = Form(...)):
    # original_name, not the name field: that one may hold an unsaved edit
    sp = _species_or_404(species)
    name = original_name
    recipe = sp.find_recipe(name)
    if recipe is None:
        raise HTTPException(status_code=404, detail=t("Rezept nicht gefunden"))
    users = sp.recipe_users(name)
    if users:
        msg = t("„{name}“ ist noch verknüpft mit: {parts}", name=name, parts=", ".join(users))
        return RedirectResponse(url=_recipes_url(msg, sp.name, name), status_code=303)
    sp.recipes.remove(recipe)
    return _recipes_redirect(t("Rezept „{name}“ ({species}) gelöscht", name=name, species=sp.name), sp.name)


@app.get("/parts.json")
def export():
    return JSONResponse([r.model_dump() for r in registry.load_all()])

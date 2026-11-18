import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator, model_validator

from .i18n import t

BASE_DIR = Path(__file__).resolve().parent.parent
# A container mounts a state directory and points these at it. Keep config.yaml
# and data/ inside one directory: save_config() writes a temp file and renames
# it over CONFIG_PATH, which fails on a bind-mounted single file (EBUSY).
STATE_DIR = Path(os.environ.get("GAMECOOLER_STATE_DIR", BASE_DIR))
CONFIG_PATH = STATE_DIR / "config.yaml"
DATA_DIR = STATE_DIR / "data"
REGISTRY_PATH = DATA_DIR / "registry.json"
SALES_PATH = DATA_DIR / "sales.json"
LEDGER_PATH = DATA_DIR / "ledger.json"
# Optional logo for the invoice letterhead, uploaded in Settings. Lives with
# the data, not in the repo: it belongs to this installation.
LOGO_PATH = DATA_DIR / "logo.png"
FONTS_DIR = Path(__file__).resolve().parent / "fonts"


class Hunter(BaseModel):
    name: str
    address: str
    phone: str
    email: str = ""
    # Bank details for the invoice's payment line; empty means no line.
    bank: str = ""
    iban: str = ""


class PrinterTarget(BaseModel):
    """A printer the app can send labels to.

    ``backend`` decides how the raster instructions reach the device:

    - ``pyusb`` / ``linux_kernel`` — directly attached, via brother_ql
    - ``network`` — a printer or print server speaking raw TCP (port 9100)
    - ``agent`` — a hardware bridge on another machine (e.g. the Mac),
      reached over HTTP because USB cannot be shared across hosts
    """

    name: str
    model: str = "QL-800"
    label: str = "39x90"
    backend: str = "pyusb"
    identifier: str = "usb://0x04f9:0x209b"
    # Cut once after each info + QR pair instead of after every label, so a
    # part's two labels come out as one strip. false = cut every label.
    cut_per_set: bool = True


PART_KINDS = {"cut": "Teilstücke", "prep": "Zubereitungen"}


class PartDefaults(BaseModel):
    # "cut" = Teilstück (Keule, Rücken), "prep" = Zubereitung (Bratwurst,
    # Hackfleisch). Only groups the lists; labels print the same either way.
    kind: str = "cut"
    price: float | None = None
    weight_kg: float | None = None
    # Printed small on the info label, e.g. for sausages. Empty = none.
    # When a recipe is linked, the label text comes from the recipe instead.
    ingredients: str | None = None
    recipe: str | None = None
    # A counted part sold as a package (the Bratwurst: 4 pieces). `price` stays
    # per kg; `piece_price` is what the package costs, and the print form
    # pre-fills "4x" with it instead of the per-kg number.
    pieces: int | None = None
    piece_price: float | None = None

    @field_validator("pieces", mode="before")
    @classmethod
    def _counted_pieces(cls, v):
        if v is None or v == "":
            return None
        try:
            n = int(str(v).strip())
        except ValueError:
            return None                       # "4,5 Stück" is no count; ignore it
        return n if n >= 1 else None

    @field_validator("ingredients", "recipe", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        if v is None:
            return None
        v = " ".join(str(v).split())
        return v or None

    @field_validator("kind", mode="before")
    @classmethod
    def _known_kind(cls, v):
        return v if v in PART_KINDS else "cut"

    @field_validator("price", "weight_kg", "piece_price", mode="before")
    @classmethod
    def _german_decimal(cls, v):
        if v is None or v == "":
            return None
        return float(str(v).replace(",", "."))


class PresetItem(BaseModel):
    part: str
    count: int


class Preset(BaseModel):
    name: str
    items: list[PresetItem]


# Units a recipe line can use. Mass and volume units convert to grams so the
# label can list ingredients by weight; the rest (pieces, casing length) can't
# be compared by weight and go last.
UNITS = {"kg": 1000.0, "g": 1.0, "l": 1000.0, "ml": 1.0, "Stk.": None, "m": None}


class RecipeItem(BaseModel):
    name: str
    amount: float | None = None
    unit: str = "g"

    @field_validator("amount", mode="before")
    @classmethod
    def _german_decimal(cls, v):
        if v is None or v == "":
            return None
        return float(str(v).replace(",", "."))

    @field_validator("unit", mode="before")
    @classmethod
    def _known_unit(cls, v):
        return v if v in UNITS else "g"

    def grams(self) -> float | None:
        factor = UNITS.get(self.unit)
        return self.amount * factor if factor is not None and self.amount is not None else None


class Recipe(BaseModel):
    name: str
    items: list[RecipeItem] = []
    notes: str = ""

    def label_text(self) -> str | None:
        """Ingredient list for the label: by weight, heaviest first, no amounts.

        Food labelling (LMIV Art. 18) lists ingredients in descending order of
        weight. Lines without a comparable weight (pieces, metres of casing,
        no amount) keep their order and come after the weighed ones.
        """
        weighed = sorted((i for i in self.items if i.grams() is not None),
                         key=lambda i: i.grams(), reverse=True)
        rest = [i for i in self.items if i.grams() is None]
        names = [i.name for i in weighed + rest if i.name.strip()]
        return ", ".join(names) or None


def _parts_dict(v) -> dict:
    """A bare number as a part's value is its price (older config shape)."""
    return {
        name: entry if isinstance(entry, (dict, PartDefaults)) else {"price": entry}
        for name, entry in (v or {}).items()
    }


class Species(BaseModel):
    """A game species with the parts and presets that belong to it.

    Parts are per species: a Wildschwein has Bratwurst, a Fasan doesn't, and
    the same part name can have a different price per species.
    """
    name: str
    parts: dict[str, PartDefaults] = {}
    presets: list[Preset] = []
    # Per species too: a Wildschwein-Salsiccia and a Reh-Salsiccia are
    # different recipes, and a part can only link a recipe of its own species.
    recipes: list[Recipe] = []
    # Wildursprungsmarke required on every label of this species. None =
    # the default: on for Wildschwein (trichinae inspection), off otherwise.
    # Set per species in Settings.
    needs_mark: bool | None = None
    # Name on the price list, where the trade word differs from the label:
    # "Schwarzwild" for Wildschwein. Empty = the species name.
    price_list_name: str = ""
    # What the price list shows, one line per entry, in this order. Each line
    # is a part name, "4x Name" for a package, or "Name oder Name2" for two
    # cuts sold at one price. Empty = this species is not on the price list.
    price_list: list[str] = []

    @field_validator("parts", mode="before")
    @classmethod
    def _scalar_is_price(cls, v):
        return _parts_dict(v)

    @field_validator("price_list_name", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        v = " ".join(str(v or "").split())
        return v

    @field_validator("price_list", mode="before")
    @classmethod
    def _clean_lines(cls, v):
        return [" ".join(str(line).split()) for line in (v or []) if str(line).strip()]

    @property
    def mark_required(self) -> bool:
        if self.needs_mark is not None:
            return self.needs_mark
        return self.name.casefold() == "wildschwein"

    @property
    def list_name(self) -> str:
        """The species name as the price list prints it."""
        return self.price_list_name or self.name

    def find_recipe(self, name: str | None) -> Recipe | None:
        return next((r for r in self.recipes if r.name == name), None) if name else None

    def ingredients_for(self, part: PartDefaults | None) -> str | None:
        """What goes on the label: the linked recipe's list, else the typed text."""
        if part is None:
            return None
        recipe = self.find_recipe(part.recipe)
        if recipe is not None:
            return recipe.label_text()
        return part.ingredients

    def recipe_users(self, name: str) -> list[str]:
        """Names of this species' parts that link the recipe."""
        return [n for n, p in self.parts.items() if p.recipe == name]

    def parts_by_kind(self) -> dict[str, dict[str, "PartDefaults"]]:
        """Parts grouped as Teilstücke / Zubereitungen, each in list order."""
        return {
            kind: {n: p for n, p in self.parts.items() if p.kind == kind}
            for kind in PART_KINDS
        }

    def price_list_lines(self) -> list[dict]:
        """The price list as rows: label, amount and price.

        One line of `price_list` becomes one row:
          - "Hackfleisch"        the part's per-kg price
          - "4x Bratwurst"       a package (pieces + piece_price)
          - "Keule oder Blatt"   two cuts at one price
          - "Keule oder Blatt = Keule ohne Knochen"
                                 a printed label different from the parts —
                                 the price comes from the named part
        A part without the price the line needs is skipped, so a half-filled
        config doesn't print a blank row.
        """
        rows = []
        for line in self.price_list:
            # Left of "=" is the printed label, right of it the part(s) the
            # price comes from. Without "=" both are the same text.
            label, has_override, source = line.partition("=")
            label = label.strip()
            counted = re.match(r"^\s*(\d+)\s*x\s+", line, re.IGNORECASE)
            count = int(counted.group(1)) if counted else None
            if counted:
                label = re.sub(r"^\s*\d+\s*x\s+", "", label, flags=re.IGNORECASE)
            names = [n.strip() for n in re.split(r"\s+oder\s+", source.strip() or label, flags=re.IGNORECASE)]
            parts = [self.parts.get(n) for n in names]
            if not names or any(p is None for p in parts):
                continue                      # validated on save; never crash a render
            first = parts[0]
            if count is not None or first.pieces is not None:
                price = first.piece_price
                if price is None:
                    continue
                amount = t("{n} Stück", n=count or first.pieces)
            else:
                price = first.price
                if price is None:
                    continue
                amount = t("pro kg")
            rows.append({"label": label if has_override else " oder ".join(names),
                         "amount": amount, "price": price})
        return rows


def price_list_rows(sp: Species) -> list[dict]:
    """The rows one species contributes to the price list.

    An empty `price_list` means the species is not on the list. Falling back to
    "everything that has a price" would put every species with a placeholder
    price on the sheet, which is never what was meant.
    """
    return sp.price_list_lines()


class Bag(BaseModel):
    """A vacuum bag size for the packing page's tare: its weight is
    subtracted from the scale's reading, so the label carries the meat."""

    name: str                 # e.g. "200×300"
    weight_g: float

    @field_validator("weight_g", mode="before")
    @classmethod
    def _german_decimal(cls, v):
        return float(str(v).replace(",", "."))


class ScaleSettings(BaseModel):
    """Bench scale on RS-232 (docs/scale.md). Empty port = no scale; the app
    is fully functional without one — the weight fields are just typed then.

    port is a pyserial URL: "/dev/tty.usbserial-*" for a USB adapter,
    "socket://<ip>:<port>" for the WLAN bridge or tools/scale_sim.py.
    """

    port: str = ""
    baudrate: int = 9600


class PaperlessSettings(BaseModel):
    """What an uploaded invoice is filed as in Paperless-ngx. Names, not IDs:
    they are looked up (and created if missing) on upload.

    The URL and token are NOT here — they come from the environment
    (PAPERLESS_URL, PAPERLESS_TOKEN; see docs/paperless.md), because this file
    is written by the settings page and the token must not end up in it."""

    document_type: str = "Rechnung"
    tags: list[str] = ["Wildbret"]
    buyer_as_correspondent: bool = True


class Config(BaseModel):
    hunters: list[Hunter]
    species: list[Species]
    printers: list[PrinterTarget] = []
    default_printer: str = ""
    best_before_months: int = 12
    dry_run: bool = True
    # Language of the printed labels and the invoice PDF. Separate from the
    # web UI's language, which follows each browser: whoever clicks print, the
    # pack goes to the same buyers.
    label_language: str = "de"
    paperless: PaperlessSettings = PaperlessSettings()
    scale: ScaleSettings = ScaleSettings()
    # Vacuum bags for the packing page's tare, heaviest use first.
    bags: list[Bag] = []

    @field_validator("label_language", mode="before")
    @classmethod
    def _known_language(cls, v):
        return v if v in ("de", "en") else "de"

    @model_validator(mode="before")
    @classmethod
    def _migrate_flat_parts(cls, data):
        """Accept the shape before parts were per species.

        There, "species" was a list of names and "parts"/"presets" were
        shared by all of them. Each species gets a copy of every part, so
        nothing is lost — unwanted combinations are deleted in the settings.
        A preset goes to every species that has all of its parts; with every
        part copied everywhere, that is every species.
        """
        if not isinstance(data, dict):
            return data
        species = data.get("species") or []
        if not species or not all(isinstance(s, str) for s in species):
            return data
        data = dict(data)
        parts = _parts_dict(data.pop("parts", None))
        presets = data.pop("presets", None) or []
        data["species"] = [
            {
                "name": name,
                "parts": {p: dict(d) for p, d in parts.items()},
                "presets": [
                    pr for pr in presets
                    if all(i.get("part") in parts for i in pr.get("items", []))
                ],
            }
            for name in species
        ]
        return data

    @model_validator(mode="before")
    @classmethod
    def _migrate_global_recipes(cls, data):
        """Accept the shape where recipes were one list shared by all species.

        A recipe goes to every species with a part that links it. A recipe no
        part links yet goes to every species, so it isn't lost; unwanted copies
        are deleted on the recipes page.
        """
        if not isinstance(data, dict) or not data.get("recipes"):
            return data
        data = dict(data)
        recipes = data.pop("recipes")
        species = [dict(sp) if isinstance(sp, dict) else sp for sp in data.get("species") or []]

        def links(sp) -> set:
            if not isinstance(sp, dict):
                return set()
            return {e.get("recipe") for e in (sp.get("parts") or {}).values() if isinstance(e, dict)}

        linked_anywhere = set().union(*(links(sp) for sp in species)) if species else set()
        for sp in species:
            if isinstance(sp, dict):
                own = links(sp)
                sp["recipes"] = list(sp.get("recipes") or []) + [
                    r for r in recipes
                    if r.get("name") in own or r.get("name") not in linked_anywhere
                ]
        data["species"] = species
        return data

    @model_validator(mode="before")
    @classmethod
    def _migrate_single_printer(cls, data):
        """Accept the pre-multi-printer config shape."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        old = data.pop("printer", None)
        if old and not data.get("printers"):
            target = dict(old)
            target.setdefault("name", "Standard")
            data["printers"] = [target]
            data.setdefault("default_printer", target["name"])
        return data

    @model_validator(mode="after")
    def _ensure_default_printer(self):
        if not self.printers:
            self.printers = [PrinterTarget(name="Standard")]
        if not any(p.name == self.default_printer for p in self.printers):
            self.default_printer = self.printers[0].name
        return self

    def find_species(self, name: str) -> Species | None:
        return next((s for s in self.species if s.name == name), None)

    def part(self, species: str, part: str) -> PartDefaults | None:
        """Settings of one part of one species, or None if it doesn't exist."""
        s = self.find_species(species)
        return s.parts.get(part) if s else None

    def ingredients_for(self, species: str, part: str) -> str | None:
        """Label ingredients of one part of one species."""
        s = self.find_species(species)
        return s.ingredients_for(s.parts.get(part)) if s else None

    def printer(self, name: str | None = None) -> PrinterTarget:
        """Resolve a printer by name, falling back to the default."""
        wanted = name or self.default_printer
        for p in self.printers:
            if p.name == wanted:
                return p
        for p in self.printers:
            if p.name == self.default_printer:
                return p
        return self.printers[0]


def load_config() -> Config:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return Config.model_validate(yaml.safe_load(f))


def _part_yaml(p: PartDefaults) -> dict:
    """Only the fields that are set, so the file stays short and readable."""
    return (
        ({"kind": p.kind} if p.kind != "cut" else {})
        | ({"price": p.price} if p.price is not None else {})
        | ({"weight_kg": p.weight_kg} if p.weight_kg is not None else {})
        | ({"pieces": p.pieces} if p.pieces is not None else {})
        | ({"piece_price": p.piece_price} if p.piece_price is not None else {})
        | ({"ingredients": p.ingredients} if p.ingredients else {})
        | ({"recipe": p.recipe} if p.recipe else {})
    )


def _recipe_yaml(r: Recipe) -> dict:
    return {
        "name": r.name,
        "items": [
            {"name": i.name, "unit": i.unit} | ({"amount": i.amount} if i.amount is not None else {})
            for i in r.items
        ],
    } | ({"notes": r.notes} if r.notes else {})


def save_config(config: Config) -> None:
    data = {
        "hunters": [h.model_dump() for h in config.hunters],
        "species": [
            {
                "name": s.name,
                "parts": {name: _part_yaml(p) for name, p in s.parts.items()},
                "presets": [p.model_dump() for p in s.presets],
                "recipes": [_recipe_yaml(r) for r in s.recipes],
            }
            | ({"needs_mark": s.needs_mark} if s.needs_mark is not None else {})
            | ({"price_list_name": s.price_list_name} if s.price_list_name else {})
            | ({"price_list": s.price_list} if s.price_list else {})
            for s in config.species
        ],
        "printers": [p.model_dump() for p in config.printers],
        "default_printer": config.default_printer,
        "best_before_months": config.best_before_months,
        "dry_run": config.dry_run,
        "label_language": config.label_language,
    }
    # only written once someone changed it, so existing files stay as they are
    if config.paperless != PaperlessSettings():
        data["paperless"] = config.paperless.model_dump()
    if config.scale != ScaleSettings():
        data["scale"] = config.scale.model_dump()
    if config.bags:
        data["bags"] = [b.model_dump() for b in config.bags]
    tmp_path = CONFIG_PATH.with_suffix(".yaml.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp_path, CONFIG_PATH)

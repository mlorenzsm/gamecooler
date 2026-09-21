import os
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator, model_validator

BASE_DIR = Path(__file__).resolve().parent.parent
# A container mounts a state directory and points these at it. Keep config.yaml
# and data/ inside one directory: save_config() writes a temp file and renames
# it over CONFIG_PATH, which fails on a bind-mounted single file (EBUSY).
STATE_DIR = Path(os.environ.get("GAMECOOLER_STATE_DIR", BASE_DIR))
CONFIG_PATH = STATE_DIR / "config.yaml"
DATA_DIR = STATE_DIR / "data"
REGISTRY_PATH = DATA_DIR / "registry.json"
SALES_PATH = DATA_DIR / "sales.json"
FONTS_DIR = Path(__file__).resolve().parent / "fonts"


class Hunter(BaseModel):
    name: str
    address: str
    phone: str
    email: str = ""


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


class PartDefaults(BaseModel):
    price: float | None = None
    weight_kg: float | None = None

    @field_validator("price", "weight_kg", mode="before")
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


class Config(BaseModel):
    hunters: list[Hunter]
    species: list[str]
    parts: dict[str, PartDefaults]
    presets: list[Preset] = []
    printers: list[PrinterTarget] = []
    default_printer: str = ""
    best_before_months: int = 12
    dry_run: bool = True

    @field_validator("parts", mode="before")
    @classmethod
    def _scalar_is_price(cls, v):
        return {
            name: entry if isinstance(entry, dict) else {"price": entry}
            for name, entry in (v or {}).items()
        }

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


def save_config(config: Config) -> None:
    data = {
        "hunters": [h.model_dump() for h in config.hunters],
        "species": config.species,
        "parts": {
            name: ({"price": p.price} if p.price is not None else {})
            | ({"weight_kg": p.weight_kg} if p.weight_kg is not None else {})
            for name, p in config.parts.items()
        },
        "presets": [p.model_dump() for p in config.presets],
        "printers": [p.model_dump() for p in config.printers],
        "default_printer": config.default_printer,
        "best_before_months": config.best_before_months,
        "dry_run": config.dry_run,
    }
    tmp_path = CONFIG_PATH.with_suffix(".yaml.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp_path, CONFIG_PATH)

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
DATA_DIR = BASE_DIR / "data"
REGISTRY_PATH = DATA_DIR / "registry.json"
SALES_PATH = DATA_DIR / "sales.json"
FONTS_DIR = Path(__file__).resolve().parent / "fonts"


class Hunter(BaseModel):
    name: str
    address: str
    phone: str
    email: str = ""


class PrinterConfig(BaseModel):
    model: str = "QL-800"
    identifier: str = "usb://0x04f9:0x209b"
    backend: str = "pyusb"
    label: str = "39x90"


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
    printer: PrinterConfig = PrinterConfig()
    best_before_months: int = 12
    dry_run: bool = True

    @field_validator("parts", mode="before")
    @classmethod
    def _scalar_is_price(cls, v):
        return {
            name: entry if isinstance(entry, dict) else {"price": entry}
            for name, entry in (v or {}).items()
        }


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
        "printer": config.printer.model_dump(),
        "best_before_months": config.best_before_months,
        "dry_run": config.dry_run,
    }
    tmp_path = CONFIG_PATH.with_suffix(".yaml.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp_path, CONFIG_PATH)

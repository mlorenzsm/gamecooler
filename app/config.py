from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
DATA_DIR = BASE_DIR / "data"
REGISTRY_PATH = DATA_DIR / "registry.json"
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


class Config(BaseModel):
    hunters: list[Hunter]
    species: list[str]
    parts: dict[str, float]
    printer: PrinterConfig = PrinterConfig()
    best_before_months: int = 12
    dry_run: bool = True

    @field_validator("parts", mode="before")
    @classmethod
    def _german_prices(cls, v):
        return {
            name: float(str(price).replace(",", "."))
            for name, price in v.items()
        }


def load_config() -> Config:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return Config.model_validate(yaml.safe_load(f))

from pathlib import Path

import yaml
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
DATA_DIR = BASE_DIR / "data"
REGISTRY_PATH = DATA_DIR / "registry.json"
FONTS_DIR = Path(__file__).resolve().parent / "fonts"


class PrinterConfig(BaseModel):
    model: str = "QL-800"
    identifier: str = "usb://0x04f9:0x209b"
    backend: str = "pyusb"
    label: str = "39x90"


class Config(BaseModel):
    hunters: list[str]
    species: list[str]
    parts: list[str]
    printer: PrinterConfig = PrinterConfig()
    dry_run: bool = True


def load_config() -> Config:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return Config.model_validate(yaml.safe_load(f))

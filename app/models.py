import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, field_validator


def parse_german_decimal(v: str | float) -> float:
    if isinstance(v, str):
        v = v.strip().replace(",", ".")
    return float(v)


class PartIn(BaseModel):
    hunter: str
    species: str
    part: str
    weight_kg: float
    price_per_kg: float

    @field_validator("weight_kg", "price_per_kg", mode="before")
    @classmethod
    def _german_decimal(cls, v):
        return parse_german_decimal(v)


class PartRecord(BaseModel):
    uuid: str
    hunter: str
    species: str
    part: str
    weight_kg: float
    price_per_kg: float
    total_price: float
    created_at: str
    printed: bool
    consumed_at: str | None = None

    @classmethod
    def from_input(cls, part: PartIn, printed: bool) -> "PartRecord":
        return cls(
            uuid=str(uuid.uuid4()),
            hunter=part.hunter,
            species=part.species,
            part=part.part,
            weight_kg=round(part.weight_kg, 3),
            price_per_kg=round(part.price_per_kg, 2),
            total_price=round(part.weight_kg * part.price_per_kg, 2),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            printed=printed,
        )


def format_de(value: float, decimals: int = 2) -> str:
    return f"{value:.{decimals}f}".replace(".", ",")

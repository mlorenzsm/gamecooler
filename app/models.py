import re
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, field_validator, model_validator

from .i18n import format_number, t


def parse_german_decimal(v: str | float) -> float:
    if isinstance(v, str):
        v = v.strip().replace(",", ".")
    return float(v)


def parse_optional_decimal(v: str | float | None) -> float | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return parse_german_decimal(v)


PIECES_RE = re.compile(r"^\s*(\d+)\s*x\s*$", re.IGNORECASE)


def parse_amount(v: str | float | None) -> tuple[float | None, int | None]:
    """Read the weight field, which also takes a piece count.

    "4,5" is a weight in kg, "5x" means five pieces. Returns (weight_kg, pieces),
    at most one of them set.
    """
    if isinstance(v, str):
        m = PIECES_RE.match(v)
        if m:
            pieces = int(m.group(1))
            if pieces < 1:
                raise ValueError(t("Stückzahl muss mindestens 1 sein"))
            return None, pieces
    return parse_optional_decimal(v), None


class PartIn(BaseModel):
    hunter: str
    species: str
    part: str
    weight_kg: float | None = None
    pieces: int | None = None
    # Per kg for weighed parts. For counted parts it is the fixed price of the
    # whole part — there is no weight to multiply it with.
    price_per_kg: float | None = None
    ingredients: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _pieces_from_weight_field(cls, data):
        if isinstance(data, dict) and isinstance(data.get("weight_kg"), str):
            weight, pieces = parse_amount(data["weight_kg"])
            data = {**data, "weight_kg": weight}
            if pieces is not None:
                data["pieces"] = pieces
        return data

    @field_validator("weight_kg", "price_per_kg", mode="before")
    @classmethod
    def _optional_german_decimal(cls, v):
        return parse_optional_decimal(v)


class PartRecord(BaseModel):
    uuid: str
    hunter: str
    species: str
    part: str
    weight_kg: float | None = None
    pieces: int | None = None
    price_per_kg: float | None = None
    total_price: float | None = None
    created_at: str
    printed: bool
    consumed_at: str | None = None
    sale_id: str | None = None
    # Copied from the part's settings when the part is created, so a reprint
    # shows the recipe that was actually used, not whatever it is today.
    ingredients: str | None = None

    @classmethod
    def from_input(cls, part: PartIn, printed: bool) -> "PartRecord":
        weight, price = part.weight_kg, part.price_per_kg
        if part.pieces is not None:
            # Counted parts have a fixed price: no per-kg price, no calculation.
            price_per_kg = None
            total = round(price, 2) if price is not None else None
        else:
            price_per_kg = round(price, 2) if price is not None else None
            total = round(weight * price, 2) if weight is not None and price is not None else None
        return cls(
            uuid=str(uuid.uuid4()),
            hunter=part.hunter,
            species=part.species,
            part=part.part,
            weight_kg=round(weight, 3) if weight is not None else None,
            pieces=part.pieces,
            price_per_kg=price_per_kg,
            total_price=total,
            ingredients=part.ingredients,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            printed=printed,
        )


def format_de(value: float | None, decimals: int = 2, lang: str | None = None) -> str:
    """A number in the current language's notation (1,5 / 1.5).

    Named for its original German-only use; the templates' "de" filter and many
    call sites use it, so the name stays.
    """
    return format_number(value, decimals, lang)


def format_amount(item, lang: str | None = None) -> str:
    """Weight or piece count of a part or sale item, as shown to people."""
    if item.pieces is not None:
        return t("{n} Stk.", lang, n=item.pieces)
    if item.weight_kg is not None:
        return f"{format_de(item.weight_kg, 3, lang)} kg"
    return "–"


class SaleItem(BaseModel):
    uuid: str
    species: str
    part: str
    weight_kg: float | None = None
    pieces: int | None = None
    price_per_kg: float | None = None
    total_price: float


class PaperlessState(BaseModel):
    """Where this sale's invoice stands in Paperless-ngx (app/paperless.py)."""

    status: str = "pending"             # pending | uploaded | failed
    task_id: str | None = None          # Paperless consumption task
    document_id: int | None = None
    error: str | None = None
    updated_at: str = ""


class Sale(BaseModel):
    sale_id: str
    number: int
    hunter: str
    buyer_name: str
    buyer_address: str
    delivery_address: str
    items: list[SaleItem]
    total: float
    created_at: str
    # None = never sent (older sales, or Paperless not configured)
    paperless: PaperlessState | None = None

    @classmethod
    def create(
        cls,
        number: int,
        hunter: str,
        buyer_name: str,
        buyer_address: str,
        delivery_address: str,
        items: list[SaleItem],
    ) -> "Sale":
        return cls(
            sale_id=str(uuid.uuid4()),
            number=number,
            hunter=hunter,
            buyer_name=buyer_name,
            buyer_address=buyer_address,
            delivery_address=delivery_address,
            items=items,
            total=round(sum(i.total_price for i in items), 2),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

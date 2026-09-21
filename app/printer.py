import logging

from brother_ql.backends.helpers import send
from brother_ql.conversion import convert
from brother_ql.raster import BrotherQLRaster
from PIL import Image

from .config import PrinterConfig

logger = logging.getLogger(__name__)


def print_labels(
    images: list[Image.Image],
    printer: PrinterConfig,
    dry_run: bool,
    chunk_size: int = 2,
) -> None:
    """Print the given label images.

    Images are sent in small batches rather than one giant blob: the whole
    job is a single USB bulk write with a 15 s timeout, and the printer's
    receive buffer cannot drain an unbounded payload before the write gives
    up — larger jobs get truncated after roughly the first two labels. Each
    batch is a self-contained job (its own initialize/invalidate/cut), so
    cutting and per-label ordering are unchanged.
    """
    if dry_run:
        logger.info("dry_run: skipping print of %d label(s)", len(images))
        return

    for start in range(0, len(images), chunk_size):
        batch = images[start : start + chunk_size]
        logger.info(
            "Printing labels %d-%d of %d", start + 1, start + len(batch), len(images)
        )
        qlr = BrotherQLRaster(printer.model)
        instructions = convert(
            qlr,
            batch,
            label=printer.label,
            rotate=90,
            cut=True,
            dither=False,
            red=False,
        )
        send(
            instructions=instructions,
            printer_identifier=printer.identifier,
            backend_identifier=printer.backend,
            blocking=True,
        )

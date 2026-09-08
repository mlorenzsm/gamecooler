import logging

from brother_ql.backends.helpers import send
from brother_ql.conversion import convert
from brother_ql.raster import BrotherQLRaster
from PIL import Image

from .config import PrinterConfig

logger = logging.getLogger(__name__)


def print_labels(images: list[Image.Image], printer: PrinterConfig, dry_run: bool) -> None:
    if dry_run:
        logger.info("dry_run: skipping print of %d label(s)", len(images))
        return

    qlr = BrotherQLRaster(printer.model)
    instructions = convert(
        qlr,
        images,
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

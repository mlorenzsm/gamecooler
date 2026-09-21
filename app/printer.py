import logging
import urllib.error
import urllib.request

from brother_ql.backends.helpers import send
from brother_ql.conversion import convert
from brother_ql.raster import BrotherQLRaster
from PIL import Image

from .config import PrinterTarget

logger = logging.getLogger(__name__)

# Backends brother_ql can drive itself (directly attached or over the network);
# "agent" is ours and goes through the hardware bridge instead.
SUPPORTED_BACKENDS = {"pyusb", "linux_kernel", "network", "agent"}

# The printer cannot drain an unbounded job: it is one bulk write with a 15 s
# timeout, and anything past roughly two labels gets truncated. Send in small
# self-contained batches so each write stays within what the buffer accepts.
CHUNK_SIZE = 2


def convert_batch(images: list[Image.Image], printer: PrinterTarget) -> bytes:
    qlr = BrotherQLRaster(printer.model)
    return convert(
        qlr,
        images,
        label=printer.label,
        rotate=90,
        cut=True,
        dither=False,
        red=False,
    )


def _send_via_agent(instructions: bytes, printer: PrinterTarget) -> None:
    """Hand the raster instructions to a hardware bridge on another host.

    USB cannot be shared between machines, so a printer attached to e.g. the
    Mac is reached through a small HTTP service running there.
    """
    url = printer.identifier.rstrip("/") + "/print"
    request = urllib.request.Request(
        url,
        data=instructions,
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status != 200:
                raise RuntimeError(f"agent returned HTTP {response.status}")
    except urllib.error.HTTPError as e:
        # the agent answered, but the printer side failed
        detail = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"print agent {url} rejected the job: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"print agent {url} unreachable: {e}") from e


def print_labels(
    images: list[Image.Image],
    printer: PrinterTarget,
    dry_run: bool,
) -> None:
    """Print the given label images on the selected printer."""
    if dry_run:
        logger.info("dry_run: skipping print of %d label(s)", len(images))
        return

    if printer.backend not in SUPPORTED_BACKENDS:
        raise ValueError(f"unknown printer backend: {printer.backend}")

    for start in range(0, len(images), CHUNK_SIZE):
        batch = images[start : start + CHUNK_SIZE]
        logger.info(
            "Printing labels %d-%d of %d on %s (%s)",
            start + 1,
            start + len(batch),
            len(images),
            printer.name,
            printer.backend,
        )
        instructions = convert_batch(batch, printer)
        if printer.backend == "agent":
            _send_via_agent(instructions, printer)
        else:
            send(
                instructions=instructions,
                printer_identifier=printer.identifier,
                backend_identifier=printer.backend,
                blocking=True,
            )

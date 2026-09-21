"""Hardware bridge for printers attached to a machine that is not the app host.

USB cannot be shared across hosts, so when the webapp runs on the Proxmox box
but the QL-800 stays plugged into the Mac, this small service runs on the Mac
and receives already-converted raster instructions over HTTP.

It deliberately knows nothing about labels, layout, or pricing — it only writes
bytes to the device. Conversion stays in one place, in the main app, so both
printing paths produce identical output.

Run it on the Mac:

    uv run uvicorn agent.main:app --host 0.0.0.0 --port 8020

Configure the device with env vars (see README):

    AGENT_PRINTER_IDENTIFIER=usb://0x04f9:0x209b
    AGENT_PRINTER_MODEL=QL-800
    AGENT_PRINTER_BACKEND=pyusb
"""

import logging
import os

from brother_ql.backends.helpers import send
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Gamecooler Druck-Agent")

PRINTER_IDENTIFIER = os.environ.get("AGENT_PRINTER_IDENTIFIER", "usb://0x04f9:0x209b")
PRINTER_BACKEND = os.environ.get("AGENT_PRINTER_BACKEND", "pyusb")


@app.get("/health")
def health():
    return JSONResponse(
        {
            "ok": True,
            "printer": PRINTER_IDENTIFIER,
            "backend": PRINTER_BACKEND,
        }
    )


@app.post("/print")
async def print_raw(request: Request):
    """Accept raster instructions and write them to the printer.

    The body is the raw instruction stream produced by brother_ql's convert().
    It is passed through untouched — the agent has no opinion about its
    contents, it just owns the USB device.
    """
    instructions = await request.body()
    if not instructions:
        raise HTTPException(status_code=400, detail="leerer Druckauftrag")

    logger.info("received %d bytes", len(instructions))
    try:
        status = send(
            instructions=instructions,
            printer_identifier=PRINTER_IDENTIFIER,
            backend_identifier=PRINTER_BACKEND,
            blocking=True,
        )
    except Exception as e:
        logger.error("print failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Druck fehlgeschlagen: {e}")

    if status.get("outcome") == "error":
        raise HTTPException(status_code=502, detail="Drucker meldete einen Fehler")

    return JSONResponse({"ok": True, "bytes": len(instructions)})

# gamecooler

A small web app for labelling cuts of game meat for the freezer.
For each cut it prints two 38×90mm labels (DK-11208) on a Brother QL-800:

1. **Info label**: hunter, species, cut, weight, price/kg, price, date
2. **QR label**: QR code with a UUID for identification

Every printed cut is registered in `data/registry.json` for accounting.

## Setup (macOS)

```sh
brew install libusb          # for the pyusb backend
uv sync
cp config.yaml.example config.yaml
```

`config.yaml` is **not** under version control — it holds the hunters with
their address and phone number. The template contains placeholders; it works
as-is for the first start (`dry_run: true` prints nothing).

Connect the printer via USB, then run:

```sh
uv run brother_ql -b pyusb discover
```

Enter the identifier it finds (e.g. `usb://0x04f9:0x209b`) in `config.yaml`
under `printers[].identifier`. Hunters, species and cuts are maintained there
as well.

## Printers

Printers are listed in `config.yaml`. `default_printer` sets which one is used
when none is selected; with two or more printers, the UI shows a selection
field.

```yaml
printers:
- name: Mac                      # connected directly to the Mac
  backend: pyusb
  identifier: usb://0x04f9:0x209b
- name: Netz                     # printer with a network port
  backend: network
  identifier: tcp://192.168.1.50:9100
- name: Mac-Agent                # Mac, but the app runs elsewhere
  backend: agent
  identifier: http://mac.local:8020
default_printer: Mac
```

`backend` determines the path to the device:

| backend | Meaning |
|---|---|
| `pyusb` | via USB on the same machine (macOS/Linux) |
| `linux_kernel` | via `/dev/usb/lp*` on the same machine (Linux) |
| `network` | printer or print server over TCP port 9100 |
| `agent` | hardware bridge on another machine (see below) |

### Print agent (USB on another machine)

USB can't be shared between machines. If the app runs e.g. on Proxmox but the
printer is still attached to the Mac, start the agent on the Mac:

```sh
uv run uvicorn agent.main:app --host 0.0.0.0 --port 8020
```

The agent only knows how to "write bytes to the device" — label layout and
conversion stay in the main app, so both paths produce identical output.
Configure the device via environment variables:

```sh
AGENT_PRINTER_IDENTIFIER=usb://0x04f9:0x209b
AGENT_PRINTER_BACKEND=pyusb
```

Check with `curl http://mac.local:8020/health`.

**Permanently, as a LaunchAgent**, so the agent survives logout, crashes and
reboots. launchd doesn't understand `~`, so the paths are filled in at install
time:

```sh
sed -e "s|__REPO__|$PWD|g" -e "s|__LOGDIR__|$HOME/Library/Logs|g" \
  deploy/gamecooler-agent.plist > ~/Library/LaunchAgents/local.gamecooler.agent.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.gamecooler.agent.plist
```

Log: `~/Library/Logs/gamecooler-agent.log`. Restart after changing the file
or the code:

```sh
launchctl kickstart -k gui/$(id -u)/local.gamecooler.agent
```

Remove: `launchctl bootout gui/$(id -u)/local.gamecooler.agent`.

A LaunchAgent only runs while the user is logged in — after a reboot, only
once the user logs in.

### State directory (container/deployment)

By default, `config.yaml` and `data/` live in the project directory.
`GAMECOOLER_STATE_DIR` redirects this — required in a container so the
settings survive:

```sh
GAMECOOLER_STATE_DIR=/var/lib/gamecooler uv run uvicorn app.main:app --port 8010
```

Important: `config.yaml` and `data/` must be in the **same** directory.
`save_config` writes a temporary file and renames it; on an individually
mounted file path this fails with `EBUSY`. So mount the directory, not the
file.

### QL-800 notes

- **Turn off Editor Lite mode**: hold the Editor Lite button until the LED
  goes out, otherwise USB raster printing fails.
- If you get "Resource busy": remove the printer from macOS System Settings
  (Printers & Scanners) and replug the USB cable — otherwise CUPS blocks the
  device.

## Running

```sh
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then open http://localhost:8000 in the browser.

`config.yaml` starts with `dry_run: true` — cuts are registered but not
printed. Set `dry_run: false` for real printing.

## Languages

German and English.

- **UI:** follows the browser language; it can be switched with DE/EN in the
  top right (remembered via cookie, i.e. per device).
- **Labels and invoice:** a separate setting (Settings → Label language
  (Einstellungen → Etikettensprache), `label_language` in `config.yaml`),
  independent of who prints — the package goes to the same buyers.
- Your own data (species, cuts, ingredients, names) is not translated.

New strings go into code and templates as `t("Deutscher Text")` — the German
text is the key — and the English version goes into `app/i18n_catalog.py`.
Check that nothing is missing:

```sh
.venv/bin/python tests/test_i18n.py
```

## Design

All styling lives in `app/static/app.css`: colour roles defined once for light
and once for dark mode (which follows the phone's setting), one type and
spacing scale, and shared components. Icons come from `app/static/icons.svg`
and are used in templates as `{{ icon('name') }}`.

Pages don't carry their own `<style>` blocks — anything with a literal
colour would ignore dark mode. For confirmations, use the app's dialog
rather than the browser's `confirm()`: put `data-confirm="Question?\n\nDetails"`
on a form or submit button (or `data-confirm-fn="name"` for a question built
from the input, `data-confirm-danger` for deletes), or `await ask(…)` from a
script. It is defined in `base.html`.

Third-party assets bundled in `app/static/`, all self-hosted so the app needs
no internet connection:

- Fonts **Geist**, **Geist Mono** and **Bricolage Grotesque** — SIL Open Font
  License 1.1, see `app/static/fonts/LICENSE-*.txt`.
  The invoice PDF uses static TrueType cuts of the same fonts in `app/fonts/`,
  built by `tools/build_pdf_fonts.py` (run it again after changing a web font).
- Icons from **Lucide** — ISC License, notice at the top of `icons.svg`.

## Accounting

- `data/registry.json` — all registered cuts
- `GET /parts.json` — export via the web app
- `data/logo.png` — optional invoice logo, uploaded in Settings → General
- `data/sales.json` — all sales; each invoice PDF is also filed in
  Paperless-ngx when configured ([Paperless upload](docs/paperless.md))

## Operations

- [Deployment on Proxmox](docs/deploy.md) — app in an LXC, Caddy as TLS
  terminator, printer stays on the Mac via the agent
- [Autodeploy](docs/autodeploy.md) — a push to `dev` updates the test
  container within five minutes, with rollback on a failed health check.
  `main` uses the same mechanism for prod; it isn't enabled there yet (set
  branch and name in `/etc/default/gamecooler-autodeploy`, start the timer).

## Planned

- [Scale integration](docs/scale.md) — take the weight directly from an RS-232
  scale (research only, scale not bought yet)
- `/version` endpoint with the running Git SHA — the health check currently
  only verifies *that* the app responds, not which commit is running.

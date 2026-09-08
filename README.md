# wildgame-labels

Kleine Webapp zum Etikettieren von Wildbret-Teilstücken fürs Einfrieren.
Druckt pro Teilstück zwei 38×90mm-Etiketten (DK-11208) auf einem Brother QL-800:

1. **Info-Etikett**: Jäger, Wildart, Teilstück, Gewicht, Preis/kg, Preis, Datum
2. **QR-Etikett**: QR-Code mit UUID zur Identifikation

Jedes gedruckte Teilstück wird in `data/registry.json` für die Buchhaltung registriert.

## Setup (macOS)

```sh
brew install libusb          # für den pyusb-Backend
uv sync
```

Drucker per USB anschließen und dann:

```sh
uv run brother_ql -b pyusb discover
```

Die gefundene Kennung (z.B. `usb://0x04f9:0x209b`) in `config.yaml` unter
`printer.identifier` eintragen. Jäger, Wildarten und Teilstücke ebenfalls
dort pflegen.

### QL-800 Hinweise

- **Editor-Lite-Modus ausschalten**: Editor-Lite-Taste gedrückt halten bis die
  LED erlischt, sonst schlägt der USB-Rasterdruck fehl.
- Falls „Resource busy": Drucker aus den macOS-Systemeinstellungen
  (Drucker & Scanner) entfernen und USB neu einstecken — CUPS blockiert
  sonst das Gerät.

## Starten

```sh
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Dann im Browser: http://localhost:8000

In `config.yaml` steht anfangs `dry_run: true` — Teilstücke werden registriert,
aber nicht gedruckt. Für echten Druck auf `dry_run: false` stellen.

## Buchhaltung

- `data/registry.json` — alle registrierten Teilstücke
- `GET /parts.json` — Export über die Webapp

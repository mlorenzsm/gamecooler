# gamecooler

Kleine Webapp zum Etikettieren von Wildbret-Teilstücken fürs Einfrieren.
Druckt pro Teilstück zwei 38×90mm-Etiketten (DK-11208) auf einem Brother QL-800:

1. **Info-Etikett**: Jäger, Wildart, Teilstück, Gewicht, Preis/kg, Preis, Datum
2. **QR-Etikett**: QR-Code mit UUID zur Identifikation

Jedes gedruckte Teilstück wird in `data/registry.json` für die Buchhaltung registriert.

## Setup (macOS)

```sh
brew install libusb          # für den pyusb-Backend
uv sync
cp config.yaml.example config.yaml
```

`config.yaml` ist **nicht** versioniert — dort stehen die Jäger mit Adresse und
Telefonnummer. Die Vorlage enthält Platzhalter; für den ersten Start reicht sie
so, wie sie ist (`dry_run: true` druckt nichts).

Drucker per USB anschließen und dann:

```sh
uv run brother_ql -b pyusb discover
```

Die gefundene Kennung (z.B. `usb://0x04f9:0x209b`) in `config.yaml` unter
`printers[].identifier` eintragen. Jäger, Wildarten und Teilstücke ebenfalls
dort pflegen.

## Drucker

Drucker stehen als Liste in `config.yaml`. `default_printer` bestimmt, welcher
ohne Auswahl verwendet wird; ab zwei Druckern erscheint in der Oberfläche ein
Auswahlfeld.

```yaml
printers:
- name: Mac                      # direkt am Mac angeschlossen
  backend: pyusb
  identifier: usb://0x04f9:0x209b
- name: Netz                     # Drucker mit Netzwerkanschluss
  backend: network
  identifier: tcp://192.168.1.50:9100
- name: Mac-Agent                # Mac, aber App läuft woanders
  backend: agent
  identifier: http://mac.local:8020
default_printer: Mac
```

`backend` bestimmt den Weg zum Gerät:

| backend | Bedeutung |
|---|---|
| `pyusb` | per USB am selben Rechner (macOS/Linux) |
| `linux_kernel` | per `/dev/usb/lp*` am selben Rechner (Linux) |
| `network` | Drucker oder Printserver über TCP Port 9100 |
| `agent` | Hardware-Bridge auf einem anderen Rechner (siehe unten) |

### Druck-Agent (USB an einem anderen Rechner)

USB lässt sich nicht zwischen Rechnern teilen. Läuft die App z.B. auf Proxmox,
der Drucker hängt aber weiter am Mac, dann auf dem Mac den Agent starten:

```sh
uv run uvicorn agent.main:app --host 0.0.0.0 --port 8020
```

Der Agent kennt nur „Bytes auf das Gerät schreiben" — Etikett-Layout und
Umrechnung bleiben in der Haupt-App, damit beide Wege identische Ausgaben
erzeugen. Gerät per Umgebungsvariablen konfigurieren:

```sh
AGENT_PRINTER_IDENTIFIER=usb://0x04f9:0x209b
AGENT_PRINTER_BACKEND=pyusb
```

Prüfen mit `curl http://mac.local:8020/health`.

**Dauerhaft als LaunchAgent**, damit der Agent Abmelden, Absturz und Neustart
übersteht. launchd kennt kein `~`, deshalb werden die Pfade beim Installieren
eingesetzt:

```sh
sed -e "s|__REPO__|$PWD|g" -e "s|__LOGDIR__|$HOME/Library/Logs|g" \
  deploy/gamecooler-agent.plist > ~/Library/LaunchAgents/local.gamecooler.agent.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.gamecooler.agent.plist
```

Log: `~/Library/Logs/gamecooler-agent.log`. Nach einer Änderung an der Datei
oder am Code neu starten:

```sh
launchctl kickstart -k gui/$(id -u)/local.gamecooler.agent
```

Entfernen: `launchctl bootout gui/$(id -u)/local.gamecooler.agent`.

Ein LaunchAgent läuft nur bei angemeldetem Benutzer — nach einem Neustart erst
ab der Anmeldung.

### Zustandsverzeichnis (Container/Deployment)

Standardmäßig liegen `config.yaml` und `data/` im Projektverzeichnis. Mit
`GAMECOOLER_STATE_DIR` lässt sich das umbiegen — nötig im Container, damit die
Einstellungen überleben:

```sh
GAMECOOLER_STATE_DIR=/var/lib/gamecooler uv run uvicorn app.main:app --port 8010
```

Wichtig: `config.yaml` und `data/` müssen im **selben** Verzeichnis liegen.
`save_config` schreibt eine temporäre Datei und benennt sie um; über einen
einzeln gemounteten Dateipfad schlägt das mit `EBUSY` fehl. Also das
Verzeichnis mounten, nicht die Datei.

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

## Betrieb

- [Deployment auf Proxmox](docs/deploy.md) — App in eine LXC, Caddy als
  TLS-Terminator, Drucker bleibt per Agent am Mac
- [Autodeploy](docs/autodeploy.md) — ein Push auf `dev` aktualisiert den
  Test-Container innerhalb von fünf Minuten, mit Rollback bei
  fehlgeschlagenem Health-Check. `main` bedient dieselbe Mechanik für Prod;
  dort ist sie noch nicht aktiviert (Branch und Name in
  `/etc/default/gamecooler-autodeploy` setzen, Timer starten).

## Geplant

- [Waagen-Anbindung](docs/scale.md) — Gewicht direkt von einer RS-232-Waage
  übernehmen (Recherche, Waage noch nicht gekauft)
- `/version`-Endpunkt mit dem laufenden Git-SHA — der Health-Check prüft
  derzeit nur, *dass* die App antwortet, nicht welcher Commit läuft.

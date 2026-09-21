# Waagen-Anbindung (geplant)

Status: **Idee / noch nicht umgesetzt.** Waage soll später gekauft und dann
angebunden werden. Dieses Dokument hält die Recherche fest, damit die
Entscheidung nicht noch einmal getroffen werden muss.

Ziel: Gewicht beim Befüllen einer Sammeldruck-Zeile bzw. beim Einzeldruck
direkt von der Waage übernehmen, statt es abzutippen.

## Modell

Gewünscht ist die **Steinberg Systems SBS-TW-15** (Metro-Marktplatz-Link,
siehe unten) — Tischwaage, **15 kg / 0,5 g**, mit **RS-232**-Schnittstelle.

> **Noch zu bestätigen:** Metro blockt direktes Abrufen der Produktseite
> (HTTP 403). Die Modell-Identifikation stammt aus der Suche, nicht aus der
> Seite selbst. Vor dem Kauf die Artikelbeschreibung prüfen, insbesondere ob
> das **RS-232-Datenkabel im Lieferumfang** ist oder separat bestellt werden
> muss (bei einigen Händlern kostet es ~20 € extra).

Steinberg Systems ist die Hausmarke von expondo (`steinbergsystems.de`), nicht
zu verwechseln mit Steinberg (Audio-Software, `steinberg.net`) — die Suche
vermischt beide Marken stark.

### Serie SBS-TW

| Modell | Bereich | Ablesbarkeit | Schnittstelle |
|---|---|---|---|
| SBS-TW-6C | 6 kg | feiner | RS-232 |
| **SBS-TW-15** | **15 kg** | **0,5 g** | **RS-232** |
| SBS-TW-30C | 30 kg | 1 g | RS-232 |

Das Suffix **C** kennzeichnet die Zählwaagen-Variante (Stückzählung) — für
diesen Anwendungsfall irrelevant. Genaue Modellnummern und Preise beim Kauf
abgleichen, das Sortiment ändert sich.

## Warum so günstig reicht

Die Genauigkeit ist für diesen Zweck massiv überdimensioniert, deshalb ist die
günstige Variante unbedenklich:

Bei 16 €/kg entspricht ein Fehler von 0,5 g auf einer 2,5-kg-Keule
**0,8 Cent**. Selbst ein Fehler von 50 g läge unter 1 €. Die Ablesbarkeit der
SBS-TW-15 (0,5 g auf 15 kg) ist rund **30.000× feiner** als für die
Preisberechnung nötig.

Der begrenzende Faktor ist ohnehin nicht die Ablesbarkeit, sondern die
Linearität und das Kriechen der Wägezelle — realistisch also einige Gramm.

15 kg reichen für einzelne Teilstücke (eine 2,5-kg-Keule passt bequem). Für ein
komplettes Reh reicht der Bereich **nicht** — dafür bräuchte es eine größere
Plattformwaage.

## Benötigte Hardware

Der Mac hat keinen seriellen Anschluss, daher zusätzlich ein
**USB→RS-232-Adapter**. Wichtig: **FTDI-Chipsatz** nehmen — CH340/PL2303
machen unter macOS erfahrungsgemäß mehr Treiber-Ärger.

Die Waage erscheint dann als `/dev/tty.usbserial-*` und lässt sich mit
`pyserial` ansprechen. (USB-Waagen mit *virtuellem* seriellen Anschluss
(CDC-ACM) verhalten sich identisch, erscheinen aber als `/dev/tty.usbmodem*`.)

## Offene Frage: ist die Waage abfragbar?

**Das ist der kritische Punkt und vor dem Kauf zu klären.**

RS-232-Waagen unterstützen typischerweise mehrere Sendemodi:

- **manuell** — nur auf Drucktaste
- **automatisch bei Stillstand** — sendet bei jeder stabilen Wägung
- **kontinuierlich** — Dauerstrom
- **auf Anfrage** — die Software schickt einen Befehl, die Waage antwortet

Für die Anbindung wird **„auf Anfrage"** gebraucht. Einige Waagen können nur
kontinuierlich senden oder nur auf Tastendruck — dann wäre kein
„Zeile wiegen"-Knopf in der Oberfläche möglich, sondern man müsste bei 12
Zeilen 12-mal physisch drücken.

Das Handbuch der SBS-TW-Serie hat einen Abschnitt zur RS-232-Übertragung, das
genaue **Befehlssyntax** ließ sich aber nicht herausziehen. Vor der
Implementierung das mitgelieferte Handbuch lesen und den Modus prüfen
(typisch: 9600 Baud, 8N1).

Falls die Waage **nicht** abfragbar ist, ist die ESP32-Variante (unten) die
bessere Wahl.

## Skizze der Anbindung

Passt in die bestehende FastAPI-App auf dem Mac, der schon den Drucker
ansteuert — kein zweiter Dienst nötig:

- `pyserial` öffnet `/dev/tty.usbserial-*`
- Lesezugriff in einem eigenen Thread (serielle Ports blockieren)
- Endpunkt z. B. `GET /scale/weight` → `{"ok": true, "weight_kg": 2.503}`
- Frontend pollt diesen Endpunkt, „Zeile wiegen"-Knopf füllt das Gewichtsfeld
- Waage in `config.yaml` konfigurierbar (Port, Baudrate, Modell)

Das Gewicht ist **nullable** — die Anbindung ist eine Bequemlichkeit, keine
Voraussetzung. Die App muss ohne Waage vollständig funktionieren.

### Vorhandene Bibliotheken

- `serial-scale-bench` — Treiber für Kern/Mettler-Toledo, erkennt zwei
  ASCII-Protokolle automatisch und bringt sogar einen FastAPI-Server mit
  (`GET /weight`, `POST /tare`, `GET /status`)
- `serial-scale-hx711` (BSD-3) — für die ESP32-Variante

## Alternativen (falls die SBS-TW-15 nicht passt)

| Option | Preis | Bereich | Schnittstelle |
|---|---|---|---|
| **ESP32 + HX711 + Wägezelle** | **~50–80 €** | 20 kg, ~5–10 g | **HTTP/WLAN** |
| Keyboard-Wedge-Waage | ~20–40 € | variiert | USB HID |
| AE ADAM CPWplus | ~229 € | Plattform | RS-232 |
| KERN VB15K2DM | ~300 € | 15 kg / 5 g | RS-232 |

**ESP32 + HX711** ist nicht nur günstiger, sondern **architektonisch die
sauberste Lösung**: liefert das Gewicht direkt über HTTP/MQTT, also kein
`/dev/tty`, kein `pyserial`, kein USB-Adapter, kein Treiber. Dafür ein
Bastelprojekt (Firmware flashen, kalibrieren, Plattform bauen).

**Keyboard-Wedge** (z. B. A&D „Quick USB") braucht null Code, ist aber
Push-only — man kann keinen Wert *abfragen*, nur auf einen Tastendruck warten.
Dazu kommt ein macOS-Problem: die Waage meldet sich als Nummernblock mit
NumLock, das macOS nicht kennt, wodurch Werte verstümmelt ankommen können. Für
einen 12-Zeilen-Ablauf ungeeignet.

## Quellen

- Metro-Marktplatz (SBS-TW-15):
  https://www.metro.de/marktplatz/product/78da41e1-e581-47fb-96a5-37cf35293abd
- Steinberg Systems: https://www.steinbergsystems.de
- `serial-scale-bench`: https://pypi.org/project/serial-scale-bench/
- `serial-scale-hx711`: https://pypi.org/project/serial-scale-hx711/

# Autodeploy

Ein Push auf `dev` aktualisiert den Test-Container innerhalb von fünf Minuten.
Dieselbe Mechanik bedient später Prod über `main` — der Unterschied ist eine
einzige Konfigurationszeile.

| Branch | Umgebung | Hostname |
|---|---|---|
| `dev` | Test-LXC | `gamecooler-test.home.arpa` |
| `main` | Prod-LXC | `wildbret.home.arpa` |

## Warum pull-basiert

Die Container stehen in einem Heimnetz (`192.168.0.x`). GitHub-Actions-Runner
laufen in der Cloud und haben keine Route dorthin. Naheliegende Alternativen
scheiden aus:

- **Self-hosted Runner im LAN.** Würde Push-Deploys mit echtem CI ermöglichen,
  aber das Repo ist **öffentlich** — wer einen PR-Workflow durchbekommt, führt
  Code im Heimnetz aus. Zu viel Angriffsfläche für ein Hobbyprojekt.
- **Eingehender Zugriff (VPN, Portfreigabe).** Ein Loch in die Heimnetz-
  Firewall für einen Etikettendrucker ist kein guter Tausch.

Deshalb dreht sich die Richtung um: ein systemd-Timer **im Container** fragt
alle fünf Minuten bei GitHub nach. Kein eingehender Zugriff, keine Secrets auf
GitHub, kein fremder Code im LAN.

## Wie ein Lauf abläuft

Der Timer startet `gamecooler-autodeploy.service`, das
`/usr/local/bin/gamecooler-autodeploy` aufruft. Das Skript:

1. Liest `GAMECOOLER_BRANCH` aus `/etc/default/gamecooler-autodeploy`.
2. Prüft, ob die App **jetzt** gesund ist. Wenn nicht: Abbruch ohne Deploy.
3. Holt den SHA von `origin/<branch>` per `git ls-remote` — das überträgt
   keine Objekte und ist der billigste Weg festzustellen, dass nichts zu tun
   ist. Ist der SHA gleich dem lokalen HEAD: fertig.
4. Ist der SHA als fehlerhaft markiert: überspringen (siehe Rollback).
5. `git fetch` + `checkout`, `uv sync --locked --no-dev`.
6. Caddyfile nach `/etc/caddy/`, `caddy validate`, `systemctl reload caddy`.
7. `gamecooler.service` nach `/etc/systemd/system/`, `daemon-reload`,
   `systemctl restart gamecooler`.
8. Health-Check (10 Versuche, 1s Abstand). Schlägt er fehl: zurückrollen.

### Warum der Health-Check *vor* dem Deploy läuft

Ist die App bereits kaputt, würde der Health-Check nach dem Update
fehlschlagen, das Skript auf den vorherigen Commit zurückrollen — also auf
einen Stand, der genauso kaputt ist — und der eigentliche Fehler wäre verdeckt.
Ein Deploy auf eine kranke Instanz macht die Diagnose schwerer, nicht leichter.

### Rollback und der Fehlermerker

Schlägt der Health-Check nach dem Update fehl, wird auf den vorherigen Commit
zurückgerollt und der neue SHA in `/var/lib/gamecooler/.autodeploy-bad`
vermerkt.

Ohne diesen Merker liefe der Timer in eine Endlosschleife: deployen,
scheitern, zurückrollen, fünf Minuten später dasselbe. Der Merker wird
gelöscht, sobald ein **anderer** SHA auftaucht — ein Fix auf dem Branch löst
ihn also von selbst auf.

### Warum der Wrapper existiert

`deploy/autodeploy.sh` liegt im Repo und wird von sich selbst überschrieben
(Schritt 5). bash liest Skripte häppchenweise über Datei-Offsets; ändert sich
die Datei währenddessen, kann die Ausführung mitten im Befehl abbrechen.

Deshalb zeigt der Timer nicht auf das Repo-Skript, sondern auf
`/usr/local/bin/gamecooler-autodeploy`, das es zuerst nach
`/run/gamecooler-autodeploy.sh` kopiert und diese Kopie ausführt. Die Kopie
wird beim Deploy nicht angefasst.

## Bootstrap

Einmalig im Container, als root. Der Container muss bereits nach
`docs/deploy.md` eingerichtet sein.

```sh
# 1. Name dieser Umgebung. Ohne das gilt der Prod-Default aus dem Caddyfile.
echo 'GAMECOOLER_HOST=gamecooler-test.home.arpa' > /etc/default/caddy

# 2. Branch, dem dieser Container folgt.
echo 'GAMECOOLER_BRANCH=dev' > /etc/default/gamecooler-autodeploy

# 3. Caddy neu starten, damit die Variable gelesen wird.
#    "reload" reicht NICHT — die EnvironmentFile wird nur beim Start gelesen.
systemctl restart caddy

# 4. Wrapper installieren.
printf '#!/bin/sh\ninstall -m755 /opt/gamecooler/deploy/autodeploy.sh /run/gamecooler-autodeploy.sh\nexec /run/gamecooler-autodeploy.sh "$@"\n' \
  > /usr/local/bin/gamecooler-autodeploy
chmod 755 /usr/local/bin/gamecooler-autodeploy

# 5. Timer installieren und starten.
cp /opt/gamecooler/deploy/gamecooler-autodeploy.service /etc/systemd/system/
cp /opt/gamecooler/deploy/gamecooler-autodeploy.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gamecooler-autodeploy.timer
```

Prüfen, dass der Name angekommen ist — **vor** dem ersten Deploy, denn ein
Deploy überschreibt das Caddyfile:

```sh
grep GAMECOOLER_HOST /etc/caddy/Caddyfile
curl -sI https://gamecooler-test.home.arpa/ | head -1     # -> HTTP/2 405
curl -s  https://gamecooler-test.home.arpa/ -o /dev/null -w '%{http_code}\n'  # -> 200
```

`405` auf `curl -I` ist erwartet: FastAPI registriert kein HEAD, `-I` sendet
aber HEAD. Der `-s`-Aufruf darunter ist der eigentliche Test.

## Prod scharf schalten

Wenn der Test-Container zufriedenstellend läuft:

```sh
echo 'GAMECOOLER_HOST=wildbret.home.arpa' > /etc/default/caddy
echo 'GAMECOOLER_BRANCH=main'            > /etc/default/gamecooler-autodeploy
systemctl restart caddy
systemctl enable --now gamecooler-autodeploy.timer
```

Das Skript selbst ändert sich nicht — die Umgebung ist reine Konfiguration.

**Vorher prüfen:** Ist `config.yaml` in Prod auf `dry_run: false` und zeigt
`backend: agent` auf den Mac? Und hat der Test-Container wirklich ein
**eigenes** Volume (`pct config <id> | grep mp0`), nicht das geteilte von Prod?

## Betrieb

```sh
# Sofort deployen, ohne auf den Timer zu warten
systemctl start gamecooler-autodeploy.service
journalctl -u gamecooler-autodeploy -n 40 --no-pager

# Nächster geplanter Lauf
systemctl list-timers gamecooler-autodeploy.timer

# Timer pausieren (z.B. während man selbst am Container arbeitet)
systemctl stop gamecooler-autodeploy.timer
```

Ein Lauf, der nichts zu tun hat, kostet ein `git ls-remote` und beendet sich
in unter einer Sekunde.

## Fehlerbilder

| Meldung im Journal | Bedeutung |
|---|---|
| `nichts zu tun — dev ist auf <sha>` | Normal, kein Deploy nötig |
| `App antwortet nicht auf ... — kein Deploy` | Die App war **vor** dem Deploy schon krank. Ursache suchen, nicht deployen. |
| `Commit <sha> ist als fehlerhaft markiert` | Der Rollback hat gegriffen. Fix auf den Branch pushen; der Merker löst sich von selbst auf. |
| `Caddyfile ungültig — nicht geladen` | Syntaxfehler im Repo-Caddyfile. Caddy läuft mit der alten Konfiguration weiter. |
| `Health-Check fehlgeschlagen nach <sha> — Rollback` | Der neue Commit startet nicht. Läuft wieder auf dem alten Stand. |
| `auch der Rollback ist nicht gesund` | Ernster Fall: beide Stände krank. Von Hand eingreifen. |
| `uv sync fehlgeschlagen` | Meist ein `uv.lock`, das nicht zum Commit passt (`--locked` bricht dann ab). Lokal `uv lock` laufen lassen und nachpushen. |

**Den Merker von Hand löschen**, wenn ein Commit zwar fehlerhaft war, aber in
Ordnung ist (z.B. der Fehler lag außerhalb des Repos):

```sh
rm /var/lib/gamecooler/.autodeploy-bad
```

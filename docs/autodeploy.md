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

1. Liest `GAMECOOLER_BRANCH` und `GAMECOOLER_HOST` aus
   `/etc/default/gamecooler-autodeploy`. Fehlt einer der beiden: `exit 2`.
2. Prüft, ob die App **jetzt** gesund ist. Wenn nicht: Abbruch ohne Deploy.
3. Prüft, ob diese Instanz unter ihrem **eigenen Namen** erreichbar ist. Wenn
   nicht: Caddyfile neu rendern, Caddy neu laden, erneut prüfen. Bleibt es
   unerreichbar: Abbruch mit `exit 1` — ohne Rollback und ohne Merker.
4. Holt den SHA von `origin/<branch>` per `git ls-remote` — das überträgt
   keine Objekte und ist der billigste Weg festzustellen, dass nichts zu tun
   ist. Ist der SHA gleich dem lokalen HEAD: fertig.
5. Ist der SHA als fehlerhaft markiert: überspringen (siehe Rollback).
6. `git fetch` + `checkout`, `uv sync --locked --no-dev`.
7. Caddyfile rendern (Platzhalter `__HOST__` → `GAMECOOLER_HOST`) nach
   `/etc/caddy/`, `caddy validate`, `systemctl reload caddy`.
8. `gamecooler.service` nach `/etc/systemd/system/`, `daemon-reload`,
   `systemctl restart gamecooler`.
9. Health-Check (10 Versuche, 1s Abstand). Schlägt er fehl: zurückrollen.

### Warum der Health-Check *vor* dem Deploy läuft

Ist die App bereits kaputt, würde der Health-Check nach dem Update
fehlschlagen, das Skript auf den vorherigen Commit zurückrollen — also auf
einen Stand, der genauso kaputt ist — und der eigentliche Fehler wäre verdeckt.
Ein Deploy auf eine kranke Instanz macht die Diagnose schwerer, nicht leichter.

### Warum zwei getrennte Health-Checks

Schritt 2 und 3 prüfen verschiedene Dinge, und nur einer von beiden ist ein
Grund zurückzurollen:

| Prüfung | Fragt | Bei Fehlschlag |
|---|---|---|
| App (`http://127.0.0.1:8010/`) | Läuft der Code? | Rollback |
| Site (`https://<HOST>/`) | Ist die Instanz unter ihrem Namen erreichbar? | Caddyfile neu rendern |

Der zweite kam später dazu, nachdem ein Container wochenlang auf dem richtigen
Commit stand und trotzdem unter seinem eigenen Namen nicht erreichbar war: das
Caddyfile trug noch den Namen der Vorgänger-Installation. Der App-Health-Check
sieht Caddy nie und meldete brav Erfolg.

Ein Fehlschlag der Site-Prüfung führt bewusst **nicht** in den Rollback: der
Rollback rendert dasselbe Caddyfile mit demselben Namen und liefe im Kreis.
Und er schreibt **keinen** Merker — der Commit ist in Ordnung, die
Konfiguration ist es nicht; ein Merker würde einen guten Commit sperren.

Die Prüfung läuft auf **jedem** Weg, auch im „nichts zu tun"- und im
übersprungenen Zweig. Stünde sie nur im Deploy-Pfad, bliebe genau der Container
stumm, der den Timer nie beschäftigt.

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

**Zuerst prüfen, im richtigen Container zu sein.** Die Hostnamen liegen eine
Transposition auseinander, und `pct enter 200` statt `107` ist schnell getippt:

```sh
hostname          # muss der TEST-Container sein, nicht der Prod-Container
pct config <id> | grep hostname     # vom Proxmox-Host aus
```

Der Fehler ist unauffällig: Prod bekäme den `dev`-Branch und würde ihn beim
nächsten Lauf ausrollen, weil `/etc/default/gamecooler-autodeploy` dort fehlt
und das Skript dann auf `dev` zurückfällt.

```sh
# 1. Repo auf den Branch bringen, dem dieser Container folgen soll.
#
#    Das MUSS vor Schritt 5 passieren: die Unit-Dateien kommen aus dem Repo,
#    ein Container auf dem alten Stand hat sie noch nicht. Als root in einem
#    Repo, das gamecooler gehört, braucht git die Ausnahme unten — sonst
#    bricht es mit "detected dubious ownership" ab.
cd /opt/gamecooler
git config --global --add safe.directory /opt/gamecooler
git fetch --depth=1 origin dev && git checkout -B dev FETCH_HEAD
ls deploy/gamecooler-autodeploy.service        # muss existieren

# 2. Branch und Name dieser Umgebung. Beide Werte sind Pflicht — fehlt einer,
#    bricht das Skript mit exit 2 ab, statt etwas Falsches auszurollen.
printf 'GAMECOOLER_BRANCH=dev\nGAMECOOLER_HOST=gamecooler-test.home.arpa\n' \
  > /etc/default/gamecooler-autodeploy

# 3. Wrapper installieren.
printf '#!/bin/sh\ninstall -m755 /opt/gamecooler/deploy/autodeploy.sh /run/gamecooler-autodeploy.sh\nexec /run/gamecooler-autodeploy.sh "$@"\n' \
  > /usr/local/bin/gamecooler-autodeploy
chmod 755 /usr/local/bin/gamecooler-autodeploy

# 4. Timer installieren und starten.
cp /opt/gamecooler/deploy/gamecooler-autodeploy.service /etc/systemd/system/
cp /opt/gamecooler/deploy/gamecooler-autodeploy.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gamecooler-autodeploy.timer

# 5. Ersten Lauf von Hand auslösen — der rendert das Caddyfile mit dem Namen
#    aus Schritt 2 und lädt Caddy neu. Ohne das stünde in /etc/caddy/Caddyfile
#    noch der Name der Vorgänger-Installation.
systemctl start gamecooler-autodeploy.service
```

**Der Name kommt nicht mehr aus Caddys Umgebung.** Frühere Fassungen setzten
`GAMECOOLER_HOST` in `/etc/default/caddy` und verließen sich darauf, dass Caddy
die Datei liest. Das tut sie nicht: die Unit des Debian-Pakets hat kein
`EnvironmentFile`, und `--environ` gibt die Umgebung nur aus. Ein
`{$GAMECOOLER_HOST:default}` im Caddyfile fällt damit **immer** auf den Default
zurück — der Test-Container lauschte unter `wildbret.home.arpa` und bekam unter
`gamecooler-test.home.arpa` kein Zertifikat (`tlsv1 alert internal error`).

Jetzt setzt `autodeploy.sh` den Namen selbst in `deploy/Caddyfile` ein
(Platzhalter `__HOST__`) und installiert das Ergebnis. Deshalb gibt es
`/etc/default/caddy` nicht mehr — **eine** Konfigurationsdatei pro Container.

Prüfen, dass der Name angekommen ist — **nach** Schritt 5, denn erst der Deploy
rendert das Caddyfile:

```sh
grep -n 'home.arpa' /etc/caddy/Caddyfile | head -2   # -> gamecooler-test…
curl -sI https://gamecooler-test.home.arpa/ | head -1     # -> HTTP/2 405
curl -s  https://gamecooler-test.home.arpa/ -o /dev/null -w '%{http_code}\n'  # -> 200
```

**Gegenprobe, dass der Container *nicht* unter dem Prod-Namen lauscht** — das
war der Fehler, und er ist von innen unsichtbar:

```sh
curl -sk --resolve wildbret.home.arpa:443:127.0.0.1 \
  -o /dev/null -w 'prod=%{http_code}\n' https://wildbret.home.arpa/
# -> 000. Kommt hier 200, bedient dieser Container den falschen Namen.
```

`405` auf `curl -I` ist erwartet: FastAPI registriert kein HEAD, `-I` sendet
aber HEAD. Der `-s`-Aufruf darunter ist der eigentliche Test.

**Aus dem Container heraus schlägt dieser `curl` fehl** (leere Ausgabe, kein
Fehler): der Name löst über Pi-hole auf die eigene Adresse auf, und die
Container-Firewall leitet das nicht zurück. Von außen, vom Laptop oder Handy,
funktioniert es. Der Deploy ist davon nicht betroffen — sein Health-Check geht
gegen `http://127.0.0.1:8010/`, nicht gegen den HTTPS-Namen.

Der erste Lauf sollte `nichts zu tun` melden, wenn Schritt 1 ausgeführt wurde —
das Repo steht dann schon auf dem aktuellen Commit. Genau das ist das erwartete
Ergebnis und belegt, dass der SHA-Vergleich greift:

## Prod scharf schalten

Wenn der Test-Container zufriedenstellend läuft:

```sh
printf 'GAMECOOLER_BRANCH=main\nGAMECOOLER_HOST=wildbret.home.arpa\n' \
  > /etc/default/gamecooler-autodeploy
systemctl enable --now gamecooler-autodeploy.timer
```

Den ersten Lauf von Hand auslösen, damit das Caddyfile sofort mit dem richtigen
Namen gerendert wird, statt bis zum nächsten Timer-Schlag zu warten:

```sh
systemctl start gamecooler-autodeploy.service
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
| `GAMECOOLER_HOST fehlt in ...` / `exit 2` | Der Name dieser Umgebung ist nicht gesetzt. Absicht: ohne ihn würde ein falscher Name installiert. |
| `Caddyfile enthält noch __HOST__` | Der Platzhalter wurde nicht ersetzt — Tippfehler im Caddyfile. Es wurde **nichts** installiert. |
| `Caddyfile gerendert für <host>` | Normal, kein Fehler: zeigt, welchen Namen der Lauf installiert hat. |
| `Health-Check fehlgeschlagen nach <sha> — Rollback` | Der neue Commit startet nicht. Läuft wieder auf dem alten Stand. |
| `auch der Rollback ist nicht gesund` | Ernster Fall: beide Stände krank. Von Hand eingreifen. |
| `uv sync fehlgeschlagen` | Meist ein `uv.lock`, das nicht zum Commit passt (`--locked` bricht dann ab). Lokal `uv lock` laufen lassen und nachpushen. |
| `fatal: $HOME not set` / `status=128` | systemd setzt `HOME` nicht (kein `User=` in der Unit). Das Skript setzt es selbst auf `/root` — taucht die Meldung trotzdem auf, ist die Unit aus einem alten Commit installiert. |
| `fatal: detected dubious ownership` | `/opt/gamecooler` gehört `gamecooler`, das Skript läuft als root. Das Skript setzt `safe.directory` selbst; bei einem Handaufruf in einem anderen Repo fehlt die Ausnahme. |
| `konnte .../.autodeploy-bad nicht schreiben` | Der Schutz gegen die Endlosschleife ist weg — der Timer rollt denselben Commit immer wieder aus und zurück. Timer sofort stoppen. |

**Den Merker von Hand löschen**, wenn ein Commit zwar fehlerhaft war, aber in
Ordnung ist (z.B. der Fehler lag außerhalb des Repos):

```sh
rm /var/lib/gamecooler/.autodeploy-bad
```

# Deployment auf Proxmox — Rollout-Plan

Status: **geplant.** Schritt-für-Schritt-Anleitung von „läuft auf dem Mac" zu
„läuft auf dem Proxmox-Cluster".

## Entscheidungen (getroffen)

| Thema | Entscheidung |
|---|---|
| DNS | **Pi-hole** — löst das Hostname-Problem |
| App-Zustand | **Proxmox-Volume** mit `backup=1` |
| TLS | **Caddy interne CA** (kein Domainkauf, kein Internet nötig) |
| Drucker | bleibt am **Mac**, per Druck-Agent |
| Mac | **schläft manchmal** → braucht Sonderbehandlung (siehe unten) |

## Architektur

```
Handy ──HTTPS──> Caddy ──> App (LXC 8010)
       │                    │  Volume: /var/lib/gamecooler
   Pi-hole                  │
   wildbret.home.arpa       └──HTTP──> Druck-Agent am Mac (8020)
   -> LXC-IP                            └──USB──> QL-800
```

## Warum HTTPS überhaupt Pflicht ist

Die Scan-Seite nutzt die Handykamera über `getUserMedia`. Browser erlauben das
nur in einem **secure context** — und `scan.html:144` prüft genau das und
startet den Scanner sonst stillschweigend nicht.

- `http://localhost` und `http://127.0.0.1` gelten **ohne TLS** als sicher.
- `http://192.168.x.y` (LAN-IP) gilt **nicht** als sicher.
- Ein HTTPS-Zertifikat, dem das Gerät **nicht vertraut**, zählt ebenfalls
  **nicht** — auch wenn man die Browserwarnung wegklickt. Deshalb reicht ein
  selbstsigniertes Zertifikat nicht; das Root-Zertifikat muss auf jedem Gerät
  installiert **und als vertrauenswürdig aktiviert** sein.

## Hostname: gelöst durch Pi-hole

Ein TLS-Zertifikat wird auf einen **Namen** ausgestellt, nicht auf eine IP.
Das Handy muss den Namen also auflösen — mit Pi-hole ist das ein Eintrag:

**Pi-hole → Local DNS → DNS Records:**

```
wildbret.home.arpa    192.168.x.y     (IP der LXC)
```

`home.arpa` ist der von RFC 8375 dafür vorgesehene Namensraum für Heimnetze —
besser als `.local` (das ist mDNS und wird von Android nicht über den
System-Resolver aufgelöst) oder `.lan` (inoffiziell).

Diesen Namen dann in `deploy/Caddyfile` eintragen. Wichtig: Der Name muss
**identisch** in Pi-hole, Caddyfile und im Browser stehen.

Falls mehrere Geräte im Haushalt den Namen brauchen: Pi-hole verteilt ihn
automatisch an alle Clients, die es als DNS nutzen.

## Warum Debian und nicht Alpine?

Naheliegende Frage, weil das Alpine-Template **3,2 MB** groß ist gegenüber
**124 MB** bei Debian 13. Nach dem Entpacken: Alpine grob 50–80 MB, Debian
realistisch ~500 MB. Der Unterschied ist also real, aber für einen Heimserver
bedeutungslos — 500 MB sind auf jeder Proxmox-Installation nichts.

Dagegen steht echter Aufwand:

- **Kein systemd.** Alpine nutzt OpenRC. Die `deploy/gamecooler.service` wäre
  ungültig und müsste als OpenRC-Init-Skript neu geschrieben werden.
  (`rc-update add ... default` statt `systemctl enable`.)
- **Kein sshd im Template.** Muss erst per `apk add openssh` nachinstalliert
  und aktiviert werden.
- **`pct`-Eigenheit.** Proxmox überschreibt bei Alpine `/etc/inittab` beim
  Start, was agetty-Autologin bricht.
- **musl statt glibc.** Grundsätzlich ein Risiko für C-Extensions — hier aber
  **geprüft und unkritisch**: alle vier C-Extensions der App
  (`pillow`, `pyyaml`, `pydantic-core`, `markupsafe`) veröffentlichen
  musllinux-Wheels für x86_64. Es müsste also nichts kompiliert werden.

Es gibt **kein** schlankeres Debian-Template: `debian-13-standard` ist die
einzige von Proxmox ausgelieferte Debian-Variante, `-minimal` existiert nicht.
Nachträgliches Ausdünnen (Docs, Locales) spart ~100–200 MB und ist den Aufwand
nicht wert.

**Empfehlung: bei `debian-13-standard` bleiben.** Die 400 MB Ersparnis sind
unsichtbar, der Aufwand (systemd-Umstellung, sshd, musl-Randfälle) ist real.
Alpine lohnt sich, wenn man viele Container betreibt oder der Host knapp ist —
beides trifft hier nicht zu.

## Rollout

### Phase 1 — LXC anlegen

```sh
pveam update
pveam available --section system | grep debian-13
```

Das listet die für **diesen** Host verfügbaren Namen — der Name hängt an der
PVE-Version und ändert sich mit jedem Template-Update. Nicht aus einer
Anleitung abschreiben, sondern aus dieser Liste nehmen:

```sh
TEMPLATE=$(pveam available --section system \
  | grep -oE 'debian-13-standard_[0-9][^ ]*_amd64\.tar\.zst' | sort | tail -1)
echo "$TEMPLATE"                                   # leer? -> siehe unten

pveam download local "$TEMPLATE"
ls /var/lib/vz/template/cache/                     # muss die Datei zeigen
```

Der `grep -oE` sucht den Dateinamen im Text, statt eine Spalte zu adressieren —
das Layout von `pveam available` ist nicht zugesichert, und `awk '{print $2}'`
liefert bei anderem Layout stillschweigend eine leere Variable.

Wenn `$TEMPLATE` leer bleibt, gibt es auf diesem Host kein Debian-13-Template.
Dann zuerst prüfen:

```sh
pveversion                       # debian-13 gibt es erst ab PVE 8
pveam update                     # Index neu holen; Ausgabe auf Fehler lesen
```

`pveam` prüft den Namen gegen den **lokalen** Index, bevor es das Netz
anfasst. Ein leerer oder veralteter Index führt deshalb zu
`template: no such template` — auch bei korrektem Namen. Das ist ein anderes
Problem als ein Tippfehler, und `pveam available` ist die einzige
verlässliche Quelle.

**Kein `*` im Template-Namen.** `local:vztmpl/...` ist eine *Storage-Referenz*,
kein Dateipfad — die Shell kann sie nicht expandieren und reicht das `*`
wörtlich an `pct` weiter. Die Fehlermeldung ist dann irreführend:

```
volume 'local:vztmpl/debian-13-standard_*.tar.zst' does not exist
```

Das sieht nach einem fehlenden Template aus, ist aber ein nicht expandierter
Glob. Deshalb den Namen einmal in `$TEMPLATE` legen und überall verwenden.
(`vztmpl` entspricht auf der `local`-Storage dem Verzeichnis
`/var/lib/vz/template/cache/` — daher der `ls`-Check.)

Vorher die beiden Netzwerte ermitteln — **nicht** raten, sonst kollidiert die
IP mit einem anderen Gerät:

```sh
ip -4 addr show vmbr0        # -> Netz und Präfix, z.B. 192.168.1.2/24
ip route | grep default      # -> Gateway, z.B. 192.168.1.1
```

Freie IP im selben Netz wählen (z.B. `192.168.1.50`) und dann:

```sh
LXC_IP=192.168.1.50          # <- freie IP, im Netz von vmbr0
GATEWAY=192.168.1.1          # <- Gateway aus "ip route"

pct create 200 "local:vztmpl/$TEMPLATE" \
  --hostname wildbret \
  --ostype debian \
  --cores 2 --memory 1024 --swap 512 \
  --rootfs local-lvm:8 \
  --net0 name=eth0,bridge=vmbr0,ip=$LXC_IP/24,gw=$GATEWAY \
  --unprivileged 1 \
  --onboot 1 \
  --start 1
```

Die zwei Variablen oben sind das Einzige, was angepasst werden muss — der Rest
ist wörtlich übernehmbar. (Ein `192.168.x.y` in der Kommandozeile ist **kein**
gültiger Platzhalter: Proxmox prüft das Feld und bricht mit
`net0.ip: invalid format` ab, weil `x` und `y` keine Ziffern sind.)

**DHCP statt fester IP?** Geht — `ip=dhcp` ist ein gültiger Wert. Proxmox
schreibt dann `iface eth0 inet dhcp` nach `/etc/network/interfaces` und der
Container holt sich Adresse *und* Gateway selbst; ein mitgegebenes `gw=`
wird in diesem Zweig ignoriert. Aber: **die Adresse muss trotzdem stabil
bleiben.** Pi-hole-Eintrag und TLS-Zertifikat hängen beide am Hostnamen und
damit an der IP — wechselt die Lease, zeigen beide ins Leere. Also nicht
einfach DHCP, sondern **DHCP mit Reservierung** (feste Lease auf die
MAC-Adresse, im Router oder in Pi-hole). Die MAC vergibt Proxmox beim Anlegen;
sie steht danach in der Container-Config:

```sh
pct config 200 | grep hwaddr
```

Diese MAC im Router als statische Lease eintragen. Für die Erstinstallation
ist das bequemer als eine IP von Hand zu wählen, weil keine Kollision möglich
ist — die feste Zusage kommt dann vom DHCP-Server.

Zu den Flags:

- `--rootfs local-lvm:8` — `8` ist die Größe in **GiB** (Obergrenze, das
  Volume wächst nicht von selbst). `local-lvm` muss der Name des
  LVM-Thin-Storage auf dem Zielknoten sein — vorher mit `pvesm status` prüfen.
- `--memory 1024 --swap 512` — beide in **MB**.
- `--unprivileged 1` ist bei `pct create` ohnehin der Standard; explizit
  hingeschrieben, damit die Absicht im Befehl steht.
- `--ostype debian` — bestimmt, welches Setup-Skript aus
  `/usr/share/lxc/config/<ostype>.common.conf` greift. Streng genommen
  optional: Proxmox liest den Typ sonst aus `/etc/os-release` des entpackten
  Templates. Explizit gesetzt dient es als Gegenprobe — passt der erkannte Typ
  nicht zum angegebenen, warnt Proxmox beim Start (`got unexpected ostype`).
  Gültige Werte u.a. `debian`, `ubuntu`, `alpine`, `unmanaged`.
- `--onboot 1 --start 1` — `--start 1` startet den Container direkt nach dem
  Anlegen, ein separates `pct start 200` ist damit nicht nötig.
- **`--features nesting=1` ist optional — hier weggelassen.** Der Flag setzt
  `lxc.apparmor.allow_nesting=1`, erlaubt `userns` und hängt die echten
  procfs/sysfs des Hosts unter `/dev/.lxc/{proc,sys}` ein, damit ein
  verschachtelter Container-Runtime (Docker) sie neu einhängen kann. Ohne den
  Flag enthält das AppArmor-Profil stattdessen explizit
  `deny mount -> /proc/` und `deny mount -> /sys/`. Für einen einzelnen
  systemd-Dienst ist er nicht nötig; Proxmox' eigene Beschreibung merkt aber
  an, dass systemd ihn zur Service-Isolation *wollen* kann. Wenn beim Start
  etwas mit `Failed to mount` oder Namespace-Fehlern auftaucht, ist das der
  erste Schalter, den man umlegt (`pct set 200 -features nesting=1`).

`$LXC_IP` aus Phase 1 notieren — die braucht Pi-hole im nächsten Schritt.

### Phase 2 — Pi-hole Eintrag

Pi-hole → *Local DNS → DNS Records*: `wildbret.home.arpa` → die IP aus
Phase 1 (im Beispiel `192.168.1.50`).

Prüfen vom Laptop:

```sh
nslookup wildbret.home.arpa        # muss die LXC-IP liefern
```

Erst weitermachen, wenn das stimmt. Ohne Namensauflösung schlägt später das
Zertifikat fehl, und der Fehler sieht dann wie ein Caddy-Problem aus.

### Phase 3 — Zustands-Volume (wichtig)

Die App schreibt zur Laufzeit nach `config.yaml` und `data/` — die
Einstellungsseite speichert Jäger, Teilstücke und Vorgaben dorthin.

```sh
pct set 200 -mp0 local-lvm:8,mp=/var/lib/gamecooler,backup=1
pct stop 200 && pct start 200      # Mount ist erst nach Neustart aktiv
```

**Volume statt Bind-Mount** — zwei Gründe:

1. **Backup.** Proxmox sichert Bind-Mounts grundsätzlich **nicht** („Device and
   bind mounts are never backed up"). Mit `backup=1` auf einem Volume ist der
   Zustand im normalen Backup-Zeitplan enthalten. Bei einem Bind-Mount müsste
   `/srv/gamecooler` separat gesichert werden — und ein grüner Backup-Job würde
   fälschlich Sicherheit vortäuschen.
2. **UID-Mapping.** Bei unprivilegierten Containern ist Container-Root auf dem
   Host ein unprivilegierter UID (typisch 100000+). Bei einem Bind-Mount muss
   man das Host-Verzeichnis per Hand `chown 100000:100000` setzen, sonst
   erscheint es im Container als `nobody` und Schreibzugriffe scheitern.
   Proxmox setzt die Rechte bei einem Volume selbst.

**Wichtig:** Immer das **Verzeichnis** mounten, nie die Datei. `save_config`
schreibt eine temporäre Datei und benennt sie per `os.replace()` um — über
einen einzeln gemounteten Dateipfad schlägt das mit `EBUSY` fehl. Deshalb zeigt
`GAMECOOLER_STATE_DIR` auf ein Verzeichnis, in dem `config.yaml` und `data/`
liegen.

Prüfen, dass das Backup greift: Backup-Zeitplan im GUI auf die LXC ansetzen
und einmal manuell laufen lassen.

### Phase 4 — App installieren

```sh
pct exec 200 -- bash

apt update && apt install -y curl ca-certificates git

# uv systemweit. Der Installer legt es sonst nach $HOME/.local/bin,
# das sieht der Dienst wegen ProtectHome=yes nicht.
#
# Achtung: UV_INSTALL_DIR ist das Verzeichnis, in dem uv SELBST landet —
# nicht dessen Elternverzeichnis. Der Installer erzwingt dabei das "flat"-
# Layout (install.sh:1228), das heißt KEIN zusätzliches /bin darunter.
#   UV_INSTALL_DIR=/usr/local      -> /usr/local/uv        (falsch)
#   UV_INSTALL_DIR=/usr/local/bin  -> /usr/local/bin/uv    (richtig)
# Passt das nicht zu ExecStart, bricht der Dienst mit
# status=203/EXEC "Unable to locate executable" ab.
curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
command -v uv && uv --version        # muss /usr/local/bin/uv zeigen

useradd --system --create-home --shell /usr/sbin/nologin gamecooler

# HTTPS, nicht SSH: das Repo ist öffentlich und der Container hat keinen
# SSH-Key. Anonymous clone funktioniert ohne Token.
git clone https://github.com/mlorenzsm/gamecooler.git /opt/gamecooler
cd /opt/gamecooler
uv sync --locked --no-dev
chown -R gamecooler:gamecooler /opt/gamecooler

# config.yaml liegt NICHT im Repo — dort stehen Jäger mit Adresse und
# Telefonnummer (siehe .gitignore). Also die Vorlage nehmen und im
# Zustands-Volume ausfüllen. Ohne die Datei legt die App eine leere an.
cp /opt/gamecooler/config.yaml.example /var/lib/gamecooler/config.yaml

# WICHTIG: das VERZEICHNIS, nicht nur die Datei. Die App legt beim ersten
# Schreiben data/ selbst an (registry.py:108, DATA_DIR.mkdir()), und dafür
# braucht sie Schreibrecht auf dem Elternverzeichnis. Nur config.yaml zu
# chownen reicht nicht — dann kommt beim ersten Druck:
#
#   File "app/registry.py", line 108, in _write
#     DATA_DIR.mkdir(exist_ok=True)
#   PermissionError: [Errno 13] Permission denied: '/var/lib/gamecooler/data'
#
# Ohne -R, gezielt die zwei Einträge. Ein "chown -R" scheitert an
# lost+found: das gehört zum ext4-Dateisystem des Volumes, und in einem
# unprivilegierten Container erscheint es als nicht zugeordneter UID (nicht
# als root), sodass selbst Container-root es nicht lesen darf:
#
#   chown: cannot read directory '/var/lib/gamecooler/lost+found': Permission denied
#
# Die Meldung sieht nach Fehlschlag aus, der Rest wird aber gesetzt. data/
# legt die App beim ersten Schreiben selbst an — es muss also nichts
# rekursiv gesetzt werden.
chown gamecooler:gamecooler /var/lib/gamecooler
chown gamecooler:gamecooler /var/lib/gamecooler/config.yaml
```

### Phase 5 — Dienst starten

```sh
cp /opt/gamecooler/deploy/gamecooler.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gamecooler
systemctl status gamecooler
```

Test **innerhalb** der LXC:

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8010/
```

Muss `200` liefern, bevor Caddy dazukommt. Die App lauscht nur auf
`127.0.0.1` — nach außen geht ausschließlich Caddy.

**Der Dienst startet uvicorn direkt aus der `.venv`, nicht über `uv run`.**
Grund: `uv` legt seinen Cache unter `$HOME/.cache/uv` an, und `ProtectHome=yes`
sperrt `$HOME` — der Dienst stirbt dann mit

```
error: Failed to initialize cache at `/home/gamecooler/.cache/uv`
  cause: failed to create directory `/home/gamecooler/.cache/uv`: Permission denied
status=2/INVALIDARGUMENT
```

und der Restart-Zähler läuft hoch. Alternativen, die man *nicht* brauchen
sollte: `ProtectHome=yes` aufgeben (schwächt die Isolation) oder
`Environment=UV_CACHE_DIR=/var/lib/gamecooler/.cache` setzen (mehr
Schreibpfade, mehr Zustand). Die `.venv` ist beim Deployment ohnehin fertig
gebaut — zur Laufzeit braucht der Dienst `uv` gar nicht.

Zwei Fehlerbilder zum Unterscheiden:

| Meldung | Ursache |
|---|---|
| `Unable to locate executable '/usr/local/bin/uv'` / `status=203/EXEC` | `uv` liegt woanders (falsches `UV_INSTALL_DIR`, siehe Phase 4) |
| `Failed to initialize cache ... Permission denied` / `status=2` | `uv run` trotz `ProtectHome=yes` — `ExecStart` auf die `.venv` umstellen |

### Phase 6 — Caddy + Zertifikat

```sh
apt install -y caddy
cp /opt/gamecooler/deploy/Caddyfile /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile   # muss "Valid configuration" sagen
systemctl restart caddy            # reload reicht nicht, siehe unten
```

**Immer `caddy validate` vor dem Restart.** Ein Syntaxfehler lässt den Dienst
sonst mit `status=1/FAILURE` sterben, und man sucht den Fehler im Zertifikat
statt in der Datei. Die Meldung nennt Zeile und Direktive, z.B.:

```
Error: adapting config using caddyfile: /etc/caddy/Caddyfile:17:
unrecognized directive: \ttls
```

Das `\t` ist der Hinweis: dort steht ein **literales** `\t` statt eines echten
Tabs. Caddy braucht echte Tabs — beim Einfügen über `sed` oder Heredoc leicht
kaputtzumachen. Gegenprobe mit `sed -n '16,20p' /etc/caddy/Caddyfile | cat -A`
— echte Tabs erscheinen als `^I`, ein literales als `\tt`.

**Der Hostname steht nicht mehr fest in der Datei**, sondern ist der
Platzhalter `__HOST__`. Wer ihn ersetzt, entscheidet die Umgebung:

```sh
# Von Hand (Phase 6 eines neuen Containers):
sed "s|__HOST__|gamecooler-test.home.arpa|g" \
  /opt/gamecooler/deploy/Caddyfile > /etc/caddy/Caddyfile

# Im Autodeploy macht das Skript selbst, aus GAMECOOLER_HOST.
```

Ohne diese Trennung würde ein Deploy in den Test-Container dessen Hostnamen mit
dem Prod-Namen überschreiben und ihn unerreichbar machen. Siehe
[autodeploy.md](autodeploy.md).

**Nicht `{$GAMECOOLER_HOST:default}` schreiben.** Das sieht eleganter aus, geht
aber schief: die Unit des Debian-Pakets hat kein `EnvironmentFile` (nur
`--environ`, das die Umgebung bloß ausgibt), also ist die Variable nie gesetzt
und der Default greift **immer**. Der Test-Container hat so eine Zeitlang unter
`wildbret.home.arpa` gelauscht und bekam unter seinem eigenen Namen kein
Zertifikat — von innen unsichtbar, weil der Health-Check nur `127.0.0.1:8010`
prüft. Gegenprobe:

```sh
curl -sk --resolve wildbret.home.arpa:443:127.0.0.1 \
  -o /dev/null -w '%{http_code}\n' https://wildbret.home.arpa/
# -> 000. Kommt 200, bedient dieser Container den falschen Namen.
```

**`tls internal` muss im Caddyfile stehen** — sonst versucht Caddy ACME. Die
Regel, wann Caddy die interne CA von selbst nimmt, ist enger, als man denkt:

| Site-Adresse | Caddy nimmt |
|---|---|
| `localhost`, `127.0.0.1` | interne CA |
| ein Name **ohne** Punkt (z.B. `wildbret`) | interne CA |
| `wildbret.home.arpa` | **ACME** — Let's Encrypt lehnt `.arpa` ab |

Die Fehlerspur ohne `tls internal`:

```
could not get certificate from issuer ... acme-v02.api.letsencrypt.org-directory
  error: HTTP 400 ... rejectedIdentifier ... "wildbret.home.arpa":
  The ACME server refuses to issue a certificate for this domain name,
  because it is forbidden by policy
could not get certificate from issuer ... acme.zerossl.com-v2-DV90
  error: ... failed getting EAB credentials: HTTP 422: caddy_legacy_user_removed
```

Beide Aussteller scheitern, Caddy wiederholt mit wachsendem Abstand
(`retrying_in` 60 → 120 → 300 → …), und **es entsteht kein Zertifikat**. Im
Browser und bei `curl` sieht man nur:

```
curl: (35) TLS connect error: error:0A000438:SSL routines::tlsv1 alert internal error
```

Das ist die Signatur für „kein Zertifikat für diesen Namen" — **nicht** für
„Zertifikat nicht vertraut". Ein vorhandenes, aber unbekanntes Zertifikat
ergibt `curl: (60) self-signed certificate`. Die Unterscheidung spart viel
Suchen: `internal error` heißt Caddy-Konfiguration, `self-signed` heißt
Geräte-Vertrauen.

Der `tls internal`-Block muss **innerhalb** des Site-Blocks stehen (siehe
`deploy/Caddyfile`). Prüfen, dass wirklich ein Zertifikat da ist:

```sh
ls -R /var/lib/caddy/.local/share/caddy/certificates/
```

Vorher war das Verzeichnis leer.

**Root-Zertifikat auf jedes Gerät:**

```sh
cat /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
```

- **iOS:** Profil installieren **und** danach unter *Einstellungen →
  Allgemein → Info → Zertifikatsvertrauenseinstellungen* das volle Vertrauen
  aktivieren. Ohne den zweiten Schritt bleibt das Zertifikat misstrauisch,
  obwohl das Profil installiert ist — und der Scanner startet nicht.
- **Android:** Apps vertrauen ab API 24 standardmäßig **nicht** den
  nutzerinstallierten CAs. Browser sind meist unproblematisch. Falls der
  Scanner im Browser läuft, ist das der relevante Fall.

**Der eigentliche Test:** `https://wildbret.home.arpa/scan` auf dem Handy
öffnen — **ohne** Zertifikatswarnung. Nur dann startet die Kamera.

### Phase 7 — Druck-Agent am Mac

```sh
cd ~/dev/private/gamecooler
uv run uvicorn agent.main:app --host 0.0.0.0 --port 8020
```

Dauerhaft als launchd-Dienst in `~/Library/LaunchAgents/`. Auf dem Mac muss
`libusb` installiert sein (`brew install libusb`).

In der `config.yaml` **auf Proxmox** (nicht am Mac):

```yaml
printers:
- name: Mac
  backend: agent
  identifier: http://<mac-ip>:8020
default_printer: Mac
```

Prüfen: `curl http://<mac-ip>:8020/health`

Der Agent hat **keine Authentifizierung** — nur im LAN erreichbar, nie ins
Internet weiterleiten. Wer ihn erreicht, kann drucken.

## Der schlafende Mac (offener Punkt)

Der Mac schläft manchmal. Dann ist der Agent nicht erreichbar und der Druck
schlägt fehl. Drei Punkte dazu:

### 1. Code-Änderung nötig: erst speichern, dann drucken

**Verifizierter Fehler.** `main.py:166-167` ruft erst `print_labels(...)` und
danach `registry.append_many(records)` auf. Schläft der Mac, schlägt der Druck
fehl und die Exception verhindert das Speichern. Nachgestellt:

```
3 Teilstücke über Sammeldruck eingegeben, Agent nicht erreichbar
  -> HTTP 500, 0 Einträge gespeichert
```

Bei einem 12-Zeilen-Sammeldruck ist die gesamte Eingabe verloren.

Das muss umgedreht werden: **erst registrieren, dann drucken.** Bei
fehlgeschlagenem Druck eine Warnung anzeigen statt eines Fehlers. Die
Nachdrucken-Funktion im Bestand existiert bereits, damit lässt sich das
problemlos nachholen.

Betrifft alle drei Stellen: `main.py:166` (Sammeldruck), `:193` (Einzeldruck),
`:208` (Nachdruck).

### 2. Wake-on-LAN — nur bedingt möglich

Ein Magic Packet vor dem Drucken könnte den Mac wecken:

```sh
wakeonlan <mac-adresse>
```

**Aber:** Das funktioniert bei Macs praktisch nur über **Ethernet**. Über
WLAN unterstützen Macs WoL nicht zuverlässig. Wenn der Mac am WLAN hängt, ist
dieser Weg versperrt — dann bleibt nur Punkt 1 und 3.

Zusätzlich nötig: *Systemeinstellungen → Energie → „Bei Netzwerkzugriff
aufwecken"* aktivieren.

### 3. Graceful Degradation

Der pragmatische Ablauf: Druck versuchen → schlägt fehl → Teilstücke sind
trotzdem gespeichert → klare Meldung „Drucker nicht erreichbar, bitte später
über Bestand nachdrucken". Das nutzt vorhandene Funktionen und verliert keine
Daten.

**Zu entscheiden:** Hängt der Mac am Ethernet (dann WoL möglich) oder am WLAN
(dann nicht)? Davon hängt ab, ob sich Punkt 2 lohnt.

## Testreihenfolge

1. `systemctl status gamecooler` → aktiv
2. `curl http://127.0.0.1:8010/` **in der LXC** → 200
3. `nslookup wildbret.home.arpa` vom Laptop → LXC-IP
4. Root-CA auf dem Handy installieren **und voll vertrauen**
5. `https://wildbret.home.arpa/` auf dem Handy → lädt ohne Warnung
6. Scan-Seite → **Kamera startet** (der eigentliche Secure-Context-Test)
7. `curl http://<mac-ip>:8020/health` → ok
8. In der App `dry_run: true`, Sammeldruck → Einträge landen im Volume
9. `dry_run: false`, echter Druck
10. Backup-Zeitplan prüfen — Volume muss enthalten sein
11. Mac schlafen lassen, Druck testen → Teilstücke müssen trotzdem
    gespeichert sein (nach der Code-Änderung aus Punkt 1)

## Alternative: öffentlich vertrauenswürdiges Zertifikat

Statt der internen CA kann Caddy ein echtes Let's-Encrypt-Zertifikat per
**DNS-01** holen — dann entfällt das Installieren der CA auf jedem Gerät.
Voraussetzungen:

- eine **eigene Domain**
- DNS bei einem Anbieter mit API (Token)
- ein **eigener Caddy-Build** via `xcaddy` mit dem passenden
  `dns.providers.*`-Modul — das offizielle Caddy-Binary enthält diese Module
  nicht

Pi-hole bleibt in beiden Fällen zuständig für die Namensauflösung
(Split-Horizon: öffentlich nicht auflösbar, intern auf die LXC-IP).

Lohnt sich, wenn viele Geräte dazukommen. Für ein paar Handys im Haushalt ist
die interne CA weniger Aufwand.

## Offene Punkte

- **Mac am Ethernet oder WLAN?** Entscheidet, ob Wake-on-LAN möglich ist.
- **Code-Änderung** „erst speichern, dann drucken" noch nicht umgesetzt.
- **Caddyfile** ist ungetestet — Caddy war lokal nicht installiert.
- **IP-SAN** als Rückfallweg, falls Pi-hole mal nicht erreichbar ist: Caddys
  interne CA kann auch ein Zertifikat für eine IP ausstellen. Noch nicht
  erprobt.
- **Waage** hängt aus demselben Grund wie der Drucker am Mac — siehe
  [scale.md](scale.md).

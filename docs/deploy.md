# Deployment on Proxmox — rollout plan

Status: **planned.** Step-by-step guide from "runs on the Mac" to
"runs on the Proxmox cluster".

## Decisions (made)

| Topic | Decision |
|---|---|
| DNS | **Pi-hole** — solves the hostname problem |
| App state | **Proxmox volume** with `backup=1` |
| TLS | **Caddy internal CA** (no domain purchase, no internet needed) |
| Printer | stays on the **Mac**, via print agent |
| Mac | **sometimes sleeps** → needs special handling (see below) |

## Architecture

```
Phone ──HTTPS──> Caddy ──> App (LXC 8010)
       │                    │  Volume: /var/lib/gamecooler
   Pi-hole                  │
   wildbret.home.arpa       └──HTTP──> print agent on the Mac (8020)
   -> LXC IP                            └──USB──> QL-800
```

## Why HTTPS is mandatory in the first place

The scan page uses the phone camera via `getUserMedia`. Browsers only allow
this in a **secure context** — and `scan.html:144` checks exactly that and
otherwise silently doesn't start the scanner.

- `http://localhost` and `http://127.0.0.1` count as secure **without TLS**.
- `http://192.168.x.y` (LAN IP) does **not** count as secure.
- An HTTPS certificate the device does **not trust** does **not** count
  either — even if you click through the browser warning. That's why a
  self-signed certificate isn't enough; the root certificate must be
  installed on every device **and enabled as trusted**.

## Hostname: solved by Pi-hole

A TLS certificate is issued for a **name**, not an IP. So the phone has to
resolve the name — with Pi-hole that's one entry:

**Pi-hole → Local DNS → DNS Records:**

```
wildbret.home.arpa    192.168.x.y     (IP of the LXC)
```

`home.arpa` is the namespace RFC 8375 reserves for home networks — better
than `.local` (that's mDNS, and Android doesn't resolve it through the
system resolver) or `.lan` (unofficial).

Then put this name into `deploy/Caddyfile`. Important: the name must be
**identical** in Pi-hole, the Caddyfile and the browser.

If several devices in the household need the name: Pi-hole hands it out
automatically to every client that uses it as DNS.

## Why Debian and not Alpine?

An obvious question, since the Alpine template is **3.2 MB** versus
**124 MB** for Debian 13. Unpacked: Alpine roughly 50–80 MB, Debian
realistically ~500 MB. So the difference is real, but meaningless for a home
server — 500 MB is nothing on any Proxmox installation.

Against that stands real effort:

- **No systemd.** Alpine uses OpenRC. `deploy/gamecooler.service` would be
  invalid and would have to be rewritten as an OpenRC init script.
  (`rc-update add ... default` instead of `systemctl enable`.)
- **No sshd in the template.** Has to be installed with `apk add openssh`
  and enabled first.
- **`pct` quirk.** With Alpine, Proxmox overwrites `/etc/inittab` on
  start, which breaks agetty autologin.
- **musl instead of glibc.** In principle a risk for C extensions — but here
  **checked and harmless**: all four of the app's C extensions
  (`pillow`, `pyyaml`, `pydantic-core`, `markupsafe`) publish
  musllinux wheels for x86_64. So nothing would need to be compiled.

There is **no** slimmer Debian template: `debian-13-standard` is the only
Debian variant Proxmox ships; `-minimal` doesn't exist. Trimming it
afterwards (docs, locales) saves ~100–200 MB and isn't worth the effort.

**Recommendation: stay with `debian-13-standard`.** The 400 MB saving is
invisible, the effort (systemd migration, sshd, musl edge cases) is real.
Alpine pays off if you run many containers or the host is tight on space —
neither applies here.

## Rollout

### Phase 1 — Create the LXC

```sh
pveam update
pveam available --section system | grep debian-13
```

This lists the names available on **this** host — the name depends on the
PVE version and changes with every template update. Don't copy it from a
guide; take it from this list:

```sh
TEMPLATE=$(pveam available --section system \
  | grep -oE 'debian-13-standard_[0-9][^ ]*_amd64\.tar\.zst' | sort | tail -1)
echo "$TEMPLATE"                                   # empty? -> see below

pveam download local "$TEMPLATE"
ls /var/lib/vz/template/cache/                     # must show the file
```

The `grep -oE` searches for the file name in the text instead of addressing a
column — the layout of `pveam available` isn't guaranteed, and
`awk '{print $2}'` silently yields an empty variable if the layout differs.

If `$TEMPLATE` stays empty, there is no Debian 13 template on this host.
Check this first:

```sh
pveversion                       # debian-13 only exists from PVE 8 on
pveam update                     # refetch the index; read the output for errors
```

`pveam` checks the name against the **local** index before it touches the
network. An empty or stale index therefore leads to
`template: no such template` — even with the correct name. That's a
different problem from a typo, and `pveam available` is the only reliable
source.

**No `*` in the template name.** `local:vztmpl/...` is a *storage reference*,
not a file path — the shell can't expand it and passes the `*` literally to
`pct`. The error message is then misleading:

```
volume 'local:vztmpl/debian-13-standard_*.tar.zst' does not exist
```

That looks like a missing template, but it's an unexpanded glob. So put the
name into `$TEMPLATE` once and use it everywhere.
(On the `local` storage, `vztmpl` corresponds to the directory
`/var/lib/vz/template/cache/` — hence the `ls` check.)

First determine the two network values — do **not** guess, or the IP will
collide with another device:

```sh
ip -4 addr show vmbr0        # -> network and prefix, e.g. 192.168.1.2/24
ip route | grep default      # -> gateway, e.g. 192.168.1.1
```

Pick a free IP in the same network (e.g. `192.168.1.50`) and then:

```sh
LXC_IP=192.168.1.50          # <- free IP, in the vmbr0 network
GATEWAY=192.168.1.1          # <- gateway from "ip route"

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

The two variables at the top are the only thing you need to adjust — the rest
can be used verbatim. (A `192.168.x.y` on the command line is **not** a valid
placeholder: Proxmox validates the field and aborts with
`net0.ip: invalid format`, because `x` and `y` aren't digits.)

**DHCP instead of a static IP?** Works — `ip=dhcp` is a valid value. Proxmox
then writes `iface eth0 inet dhcp` to `/etc/network/interfaces` and the
container obtains its address *and* gateway itself; a supplied `gw=` is
ignored in this branch. But: **the address must still stay stable.** The
Pi-hole entry and the TLS certificate both hang on the hostname and thus on
the IP — if the lease changes, both point nowhere. So not plain DHCP, but
**DHCP with a reservation** (a fixed lease on the MAC address, in the router
or in Pi-hole). Proxmox assigns the MAC on creation; afterwards it's in the
container config:

```sh
pct config 200 | grep hwaddr
```

Enter this MAC as a static lease in the router. For the initial install
that's more convenient than picking an IP by hand, because no collision is
possible — the fixed assignment then comes from the DHCP server.

About the flags:

- `--rootfs local-lvm:8` — `8` is the size in **GiB** (an upper limit; the
  volume doesn't grow by itself). `local-lvm` must be the name of the
  LVM-thin storage on the target node — check with `pvesm status` first.
- `--memory 1024 --swap 512` — both in **MB**.
- `--unprivileged 1` is the default for `pct create` anyway; spelled out
  so the intent is visible in the command.
- `--ostype debian` — determines which setup script from
  `/usr/share/lxc/config/<ostype>.common.conf` applies. Strictly speaking
  optional: otherwise Proxmox reads the type from `/etc/os-release` of the
  unpacked template. Set explicitly, it serves as a cross-check — if the
  detected type doesn't match the given one, Proxmox warns on start
  (`got unexpected ostype`). Valid values include `debian`, `ubuntu`,
  `alpine`, `unmanaged`.
- `--onboot 1 --start 1` — `--start 1` starts the container right after
  creation, so a separate `pct start 200` isn't needed.
- **`--features nesting=1` is optional — omitted here.** The flag sets
  `lxc.apparmor.allow_nesting=1`, allows `userns` and mounts the host's real
  procfs/sysfs under `/dev/.lxc/{proc,sys}`, so that a nested container
  runtime (Docker) can remount them. Without the flag, the AppArmor profile
  instead explicitly contains `deny mount -> /proc/` and
  `deny mount -> /sys/`. A single systemd service doesn't need it; Proxmox's
  own description does note, though, that systemd may *want* it for service
  isolation. If something like `Failed to mount` or namespace errors shows up
  on start, this is the first switch to flip
  (`pct set 200 -features nesting=1`).

Note down `$LXC_IP` from Phase 1 — Pi-hole needs it in the next step.

### Phase 2 — Pi-hole entry

Pi-hole → *Local DNS → DNS Records*: `wildbret.home.arpa` → the IP from
Phase 1 (in the example `192.168.1.50`).

Check from the laptop:

```sh
nslookup wildbret.home.arpa        # must return the LXC IP
```

Only continue once this is right. Without name resolution the certificate
fails later, and the error then looks like a Caddy problem.

### Phase 3 — State volume (important)

At runtime the app writes to `config.yaml` and `data/` — the settings page
saves hunters, cuts and presets there.

```sh
pct set 200 -mp0 local-lvm:8,mp=/var/lib/gamecooler,backup=1
pct stop 200 && pct start 200      # the mount is only active after a restart
```

**Volume instead of bind mount** — two reasons:

1. **Backup.** Proxmox never backs up bind mounts ("Device and
   bind mounts are never backed up"). With `backup=1` on a volume, the
   state is part of the normal backup schedule. With a bind mount,
   `/srv/gamecooler` would have to be backed up separately — and a green
   backup job would give a false sense of safety.
2. **UID mapping.** In unprivileged containers, container root is an
   unprivileged UID on the host (typically 100000+). With a bind mount you
   have to `chown 100000:100000` the host directory by hand, otherwise it
   shows up as `nobody` inside the container and writes fail. With a volume,
   Proxmox sets the permissions itself.

**Important:** Always mount the **directory**, never the file. `save_config`
writes a temporary file and renames it with `os.replace()` — across a
single mounted file path that fails with `EBUSY`. That's why
`GAMECOOLER_STATE_DIR` points to a directory containing `config.yaml` and
`data/`.

Check that the backup works: add the LXC to a backup schedule in the GUI and
run it once manually.

### Phase 4 — Install the app

```sh
pct exec 200 -- bash

apt update && apt install -y curl ca-certificates git

# uv system-wide. Otherwise the installer puts it in $HOME/.local/bin,
# which the service can't see because of ProtectHome=yes.
#
# Note: UV_INSTALL_DIR is the directory uv ITSELF ends up in —
# not its parent directory. The installer enforces the "flat"
# layout (install.sh:1228), i.e. NO extra /bin underneath.
#   UV_INSTALL_DIR=/usr/local      -> /usr/local/uv        (wrong)
#   UV_INSTALL_DIR=/usr/local/bin  -> /usr/local/bin/uv    (right)
# If this doesn't match ExecStart, the service aborts with
# status=203/EXEC "Unable to locate executable".
curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
command -v uv && uv --version        # must show /usr/local/bin/uv

useradd --system --create-home --shell /usr/sbin/nologin gamecooler

# HTTPS, not SSH: the repo is public and the container has no
# SSH key. An anonymous clone works without a token.
git clone https://github.com/mlorenzsm/gamecooler.git /opt/gamecooler
cd /opt/gamecooler
uv sync --locked --no-dev
chown -R gamecooler:gamecooler /opt/gamecooler

# config.yaml is NOT in the repo — it holds hunters with address and
# phone number (see .gitignore). So take the template and fill it in
# on the state volume. Without the file the app creates an empty one.
cp /opt/gamecooler/config.yaml.example /var/lib/gamecooler/config.yaml

# IMPORTANT: the DIRECTORY, not just the file. The app creates data/
# itself on first write (registry.py:108, DATA_DIR.mkdir()), and for that
# it needs write access to the parent directory. Chowning only config.yaml
# isn't enough — then the first print gives:
#
#   File "app/registry.py", line 108, in _write
#     DATA_DIR.mkdir(exist_ok=True)
#   PermissionError: [Errno 13] Permission denied: '/var/lib/gamecooler/data'
#
# Without -R, targeting the two entries. A "chown -R" fails on
# lost+found: it belongs to the volume's ext4 filesystem, and in an
# unprivileged container it shows up as an unmapped UID (not as
# root), so even container root may not read it:
#
#   chown: cannot read directory '/var/lib/gamecooler/lost+found': Permission denied
#
# The message looks like a failure, but everything else does get set. The
# app creates data/ itself on first write — so nothing needs to be set
# recursively.
chown gamecooler:gamecooler /var/lib/gamecooler
chown gamecooler:gamecooler /var/lib/gamecooler/config.yaml
```

### Phase 5 — Start the service

```sh
cp /opt/gamecooler/deploy/gamecooler.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gamecooler
systemctl status gamecooler
```

Test **inside** the LXC:

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8010/
```

Must return `200` before Caddy comes in. The app only listens on
`127.0.0.1` — only Caddy faces the outside.

**The service starts uvicorn directly from the `.venv`, not via `uv run`.**
Reason: `uv` puts its cache under `$HOME/.cache/uv`, and `ProtectHome=yes`
locks `$HOME` — the service then dies with

```
error: Failed to initialize cache at `/home/gamecooler/.cache/uv`
  cause: failed to create directory `/home/gamecooler/.cache/uv`: Permission denied
status=2/INVALIDARGUMENT
```

and the restart counter climbs. Alternatives you *shouldn't* need:
dropping `ProtectHome=yes` (weakens isolation) or setting
`Environment=UV_CACHE_DIR=/var/lib/gamecooler/.cache` (more write paths,
more state). The `.venv` is fully built at deployment time anyway — at
runtime the service doesn't need `uv` at all.

Two failure patterns to tell apart:

| Message | Cause |
|---|---|
| `Unable to locate executable '/usr/local/bin/uv'` / `status=203/EXEC` | `uv` is somewhere else (wrong `UV_INSTALL_DIR`, see Phase 4) |
| `Failed to initialize cache ... Permission denied` / `status=2` | `uv run` despite `ProtectHome=yes` — switch `ExecStart` to the `.venv` |

### Phase 6 — Caddy + certificate

```sh
apt install -y caddy
cp /opt/gamecooler/deploy/Caddyfile /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile   # must say "Valid configuration"
systemctl restart caddy            # reload isn't enough, see below
```

**Always run `caddy validate` before the restart.** Otherwise a syntax error
kills the service with `status=1/FAILURE`, and you look for the problem in
the certificate instead of the file. The message names line and directive,
e.g.:

```
Error: adapting config using caddyfile: /etc/caddy/Caddyfile:17:
unrecognized directive: \ttls
```

The `\t` is the clue: there's a **literal** `\t` instead of a real tab.
Caddy needs real tabs — easy to break when inserting via `sed` or a heredoc.
Cross-check with `sed -n '16,20p' /etc/caddy/Caddyfile | cat -A`
— real tabs show up as `^I`, a literal one as `\tt`.

**The hostname is no longer hard-coded in the file**; it's the placeholder
`__HOST__`. The environment decides who replaces it:

```sh
# By hand (Phase 6 of a new container):
sed "s|__HOST__|gamecooler-test.home.arpa|g" \
  /opt/gamecooler/deploy/Caddyfile > /etc/caddy/Caddyfile

# In autodeploy the script does this itself, from GAMECOOLER_HOST.
```

Without this separation, a deploy to the test container would overwrite its
hostname with the prod name and make it unreachable. See
[autodeploy.md](autodeploy.md).

**Don't write `{$GAMECOOLER_HOST:default}`.** It looks more elegant but goes
wrong: the Debian package's unit has no `EnvironmentFile` (only
`--environ`, which merely prints the environment), so the variable is never
set and the default **always** applies. That's how the test container spent
a while listening on `wildbret.home.arpa` and got no certificate under its
own name — invisible from inside, because the health check only checks
`127.0.0.1:8010`. Cross-check:

```sh
curl -sk --resolve wildbret.home.arpa:443:127.0.0.1 \
  -o /dev/null -w '%{http_code}\n' https://wildbret.home.arpa/
# -> 000. If you get 200, this container is serving the wrong name.
```

**`tls internal` must be in the Caddyfile** — otherwise Caddy tries ACME. The
rule for when Caddy picks the internal CA on its own is narrower than you'd
think:

| Site address | Caddy uses |
|---|---|
| `localhost`, `127.0.0.1` | internal CA |
| a name **without** a dot (e.g. `wildbret`) | internal CA |
| `wildbret.home.arpa` | **ACME** — Let's Encrypt rejects `.arpa` |

The error trail without `tls internal`:

```
could not get certificate from issuer ... acme-v02.api.letsencrypt.org-directory
  error: HTTP 400 ... rejectedIdentifier ... "wildbret.home.arpa":
  The ACME server refuses to issue a certificate for this domain name,
  because it is forbidden by policy
could not get certificate from issuer ... acme.zerossl.com-v2-DV90
  error: ... failed getting EAB credentials: HTTP 422: caddy_legacy_user_removed
```

Both issuers fail, Caddy retries with increasing intervals
(`retrying_in` 60 → 120 → 300 → …), and **no certificate is produced**. In
the browser and with `curl` all you see is:

```
curl: (35) TLS connect error: error:0A000438:SSL routines::tlsv1 alert internal error
```

That's the signature of "no certificate for this name" — **not** of
"certificate not trusted". An existing but unknown certificate gives
`curl: (60) self-signed certificate`. The distinction saves a lot of
searching: `internal error` means Caddy configuration, `self-signed` means
device trust.

The `tls internal` block must be **inside** the site block (see
`deploy/Caddyfile`). Check that a certificate actually exists:

```sh
ls -R /var/lib/caddy/.local/share/caddy/certificates/
```

Before, the directory was empty.

**Root certificate onto every device:**

```sh
cat /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
```

- **iOS:** Install the profile **and** then enable full trust under
  *Settings → General → About → Certificate Trust Settings*. Without the
  second step the certificate stays untrusted even though the profile is
  installed — and the scanner doesn't start.
- **Android:** From API 24 on, apps do **not** trust user-installed CAs by
  default. Browsers are usually fine. If the scanner runs in the browser,
  that's the relevant case.

**The real test:** open `https://wildbret.home.arpa/scan` on the phone —
**without** a certificate warning. Only then does the camera start.

### Phase 7 — Print agent on the Mac

```sh
cd ~/dev/private/gamecooler
uv run uvicorn agent.main:app --host 0.0.0.0 --port 8020
```

Permanently as a launchd service in `~/Library/LaunchAgents/`. The Mac needs
`libusb` installed (`brew install libusb`).

In the `config.yaml` **on Proxmox** (not on the Mac):

```yaml
printers:
- name: Mac
  backend: agent
  identifier: http://<mac-ip>:8020
default_printer: Mac
```

Check: `curl http://<mac-ip>:8020/health`

The agent has **no authentication** — reachable only on the LAN, never
forward it to the internet. Anyone who can reach it can print.

## The sleeping Mac (open issue)

The Mac sometimes sleeps. Then the agent is unreachable and printing fails.
Three points on this:

### 1. Code change needed: save first, then print

**Verified bug.** `main.py:166-167` first calls `print_labels(...)` and
then `registry.append_many(records)`. If the Mac is asleep, printing fails
and the exception prevents saving. Reproduced:

```
3 cuts entered via bulk print, agent unreachable
  -> HTTP 500, 0 entries saved
```

With a 12-row bulk print, the entire input is lost.

This must be reversed: **register first, then print.** If printing fails,
show a warning instead of an error. The Reprint function in Stock already
exists, so this can easily be caught up on later.

Affects all three places: `main.py:166` (bulk print), `:193` (single print),
`:208` (reprint).

### 2. Wake-on-LAN — only partly possible

A magic packet before printing could wake the Mac:

```sh
wakeonlan <mac-address>
```

**But:** on Macs this practically only works over **Ethernet**. Over Wi-Fi,
Macs don't support WoL reliably. If the Mac is on Wi-Fi, this route is
closed — then only points 1 and 3 remain.

Also needed: enable *System Settings → Energy → "Wake for network
access"*.

### 3. Graceful degradation

The pragmatic flow: try to print → fails → the cuts are saved anyway →
clear message "Printer unreachable, please reprint later from Stock". This
uses existing functions and loses no data.

**To decide:** Is the Mac on Ethernet (then WoL is possible) or on Wi-Fi
(then not)? That determines whether point 2 is worth it.

## Test order

1. `systemctl status gamecooler` → active
2. `curl http://127.0.0.1:8010/` **inside the LXC** → 200
3. `nslookup wildbret.home.arpa` from the laptop → LXC IP
4. Install the root CA on the phone **and trust it fully**
5. `https://wildbret.home.arpa/` on the phone → loads without a warning
6. Scan page → **camera starts** (the actual secure-context test)
7. `curl http://<mac-ip>:8020/health` → ok
8. In the app `dry_run: true`, bulk print → entries end up in the volume
9. `dry_run: false`, real print
10. Check the backup schedule — the volume must be included
11. Let the Mac sleep, test printing → cuts must still be saved
    (after the code change from point 1)

## Alternative: publicly trusted certificate

Instead of the internal CA, Caddy can obtain a real Let's Encrypt certificate
via **DNS-01** — then there's no need to install the CA on every device.
Prerequisites:

- your **own domain**
- DNS with a provider that has an API (token)
- a **custom Caddy build** via `xcaddy` with the matching
  `dns.providers.*` module — the official Caddy binary doesn't include
  these modules

In both cases Pi-hole stays responsible for name resolution
(split horizon: not resolvable publicly, internally pointing to the LXC IP).

Worth it if many devices get added. For a few phones in the household, the
internal CA is less effort.

## Open issues

- **Mac on Ethernet or Wi-Fi?** Decides whether Wake-on-LAN is possible.
- **Code change** "save first, then print" not implemented yet.
- **Caddyfile** is untested — Caddy wasn't installed locally.
- **IP SAN** as a fallback in case Pi-hole is ever unreachable: Caddy's
  internal CA can also issue a certificate for an IP. Not tried yet.
- **Scale** hangs off the Mac for the same reason as the printer — see
  [scale.md](scale.md).

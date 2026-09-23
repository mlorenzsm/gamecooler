# Autodeploy

A push to `dev` updates the test container within five minutes. The same
mechanism will later serve prod via `main` — the difference is a single
configuration line.

| Branch | Environment | Hostname |
|---|---|---|
| `dev` | Test LXC | `gamecooler-test.home.arpa` |
| `main` | Prod LXC | `wildbret.home.arpa` |

## Why pull-based

The containers live on a home network (`192.168.0.x`). GitHub Actions runners
run in the cloud and have no route there. The obvious alternatives are ruled
out:

- **Self-hosted runner on the LAN.** Would allow push deploys with real CI,
  but the repo is **public** — anyone who gets a PR workflow through runs
  code on the home network. Too much attack surface for a hobby project.
- **Inbound access (VPN, port forwarding).** A hole in the home network
  firewall for a label printer isn't a good trade.

So the direction is reversed: a systemd timer **in the container** polls
GitHub every five minutes. No inbound access, no secrets on GitHub, no
foreign code on the LAN.

## How a run works

The timer starts `gamecooler-autodeploy.service`, which calls
`/usr/local/bin/gamecooler-autodeploy`. The script:

1. Reads `GAMECOOLER_BRANCH` and `GAMECOOLER_HOST` from
   `/etc/default/gamecooler-autodeploy`. If either is missing: `exit 2`.
2. Checks whether the app is healthy **right now**. If not: abort without deploying.
3. Checks whether this instance is reachable under its **own name**. If
   not: re-render the Caddyfile, reload Caddy, check again. If it stays
   unreachable: abort with `exit 1` — without rollback and without a marker.
4. Gets the SHA of `origin/<branch>` via `git ls-remote` — this transfers
   no objects and is the cheapest way to find out there's nothing to do.
   If the SHA equals the local HEAD: done.
5. If the SHA is marked as bad: skip (see Rollback).
6. `git fetch` + `checkout`, `uv sync --locked --no-dev`.
7. Render the Caddyfile (placeholder `__HOST__` → `GAMECOOLER_HOST`) into
   `/etc/caddy/`, `caddy validate`, `systemctl reload caddy`.
8. `gamecooler.service` into `/etc/systemd/system/`, `daemon-reload`,
   `systemctl restart gamecooler`.
9. Health check (10 attempts, 1s apart). If it fails: roll back.

### Why the health check runs *before* the deploy

If the app is already broken, the health check after the update would fail,
the script would roll back to the previous commit — i.e. to a state that is
just as broken — and the actual fault would be masked. Deploying onto a sick
instance makes diagnosis harder, not easier.

### Why two separate health checks

Steps 2 and 3 check different things, and only one of them is a reason to
roll back:

| Check | Asks | On failure |
|---|---|---|
| App (`http://127.0.0.1:8010/`) | Is the code running? | Rollback |
| Site (`https://<HOST>/`) | Is the instance reachable under its name? | Re-render the Caddyfile |

The second one was added later, after a container sat on the right commit for
weeks and was still unreachable under its own name: the Caddyfile still
carried the name of the previous installation. The app health check never
sees Caddy and dutifully reported success.

A failed site check deliberately does **not** lead to a rollback: the
rollback renders the same Caddyfile with the same name and would go in
circles. And it writes **no** marker — the commit is fine, the configuration
isn't; a marker would block a good commit.

The check runs on **every** path, including the "nothing to do" branch and
the skipped branch. If it were only in the deploy path, exactly the container
that never keeps the timer busy would stay silent.

### Rollback and the bad-commit marker

If the health check fails after the update, the script rolls back to the
previous commit and records the new SHA in
`/var/lib/gamecooler/.autodeploy-bad`.

Without this marker the timer would run into an endless loop: deploy, fail,
roll back, the same thing five minutes later. The marker is deleted as soon
as a **different** SHA appears — so a fix on the branch clears it
automatically.

### Why the wrapper exists

`deploy/autodeploy.sh` lives in the repo and gets overwritten by itself
(step 6). bash reads scripts piecemeal via file offsets; if the file changes
in the meantime, execution can break off mid-command.

That's why the timer doesn't point at the repo script but at
`/usr/local/bin/gamecooler-autodeploy`, which first copies it to
`/run/gamecooler-autodeploy.sh` and runs that copy. The copy isn't touched
by the deploy.

## Bootstrap

Once, inside the container, as root. The container must already be set up
according to `docs/deploy.md`.

**First check that you're in the right container.** The hostnames are one
transposition apart, and `pct enter 200` instead of `107` is quickly typed:

```sh
hostname          # must be the TEST container, not the prod container
pct config <id> | grep hostname     # from the Proxmox host
```

A misconfigured container no longer deploys silently; it aborts with
`exit 2`: if `GAMECOOLER_HOST` is missing, nothing is rolled out. The wrong
container thus stays put instead of rolling out foreign code — verifiable in
the journal. The `hostname` check above is still the faster route.

```sh
# 1. Bring the repo onto the branch this container should follow.
#
#    This MUST happen before step 5: the unit files come from the repo,
#    a container on the old state doesn't have them yet. As root in a
#    repo owned by gamecooler, git needs the exception below — otherwise
#    it aborts with "detected dubious ownership".
cd /opt/gamecooler
git config --global --add safe.directory /opt/gamecooler
git fetch --depth=1 origin dev && git checkout -B dev FETCH_HEAD
ls deploy/gamecooler-autodeploy.service        # must exist

# 2. Branch and name of this environment. Both values are required — if one
#    is missing, the script aborts with exit 2 instead of rolling out the wrong thing.
printf 'GAMECOOLER_BRANCH=dev\nGAMECOOLER_HOST=gamecooler-test.home.arpa\n' \
  > /etc/default/gamecooler-autodeploy

# 3. Install the wrapper.
printf '#!/bin/sh\ninstall -m755 /opt/gamecooler/deploy/autodeploy.sh /run/gamecooler-autodeploy.sh\nexec /run/gamecooler-autodeploy.sh "$@"\n' \
  > /usr/local/bin/gamecooler-autodeploy
chmod 755 /usr/local/bin/gamecooler-autodeploy

# 4. Install and start the timer.
cp /opt/gamecooler/deploy/gamecooler-autodeploy.service /etc/systemd/system/
cp /opt/gamecooler/deploy/gamecooler-autodeploy.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gamecooler-autodeploy.timer

# 5. Trigger the first run by hand — it renders the Caddyfile with the name
#    from step 2 and reloads Caddy. Without it, /etc/caddy/Caddyfile would
#    still contain the name of the previous installation.
systemctl start gamecooler-autodeploy.service
```

**The name no longer comes from Caddy's environment.** Earlier versions set
`GAMECOOLER_HOST` in `/etc/default/caddy` and relied on Caddy reading the
file. It doesn't: the Debian package's unit has no `EnvironmentFile`, and
`--environ` only prints the environment. A `{$GAMECOOLER_HOST:default}` in
the Caddyfile therefore **always** falls back to the default — the test
container listened on `wildbret.home.arpa` and got no certificate for
`gamecooler-test.home.arpa` (`tlsv1 alert internal error`).

Now `autodeploy.sh` inserts the name into `deploy/Caddyfile` itself
(placeholder `__HOST__`) and installs the result. That's why
`/etc/default/caddy` no longer exists — **one** configuration file per container.

Check that the name has arrived — **after** step 5, because only the deploy
renders the Caddyfile:

```sh
grep -n 'home.arpa' /etc/caddy/Caddyfile | head -2   # -> gamecooler-test…
curl -sI https://gamecooler-test.home.arpa/ | head -1     # -> HTTP/2 405
curl -s  https://gamecooler-test.home.arpa/ -o /dev/null -w '%{http_code}\n'  # -> 200
```

**Cross-check that the container is *not* listening under the prod name** —
that was the bug, and it's invisible from inside:

```sh
curl -sk --resolve wildbret.home.arpa:443:127.0.0.1 \
  -o /dev/null -w 'prod=%{http_code}\n' https://wildbret.home.arpa/
# -> 000. If you get 200 here, this container is serving the wrong name.
```

`405` on `curl -I` is expected: FastAPI doesn't register HEAD, but `-I`
sends HEAD. The `-s` call below it is the real test.

**From inside the container this `curl` fails** (empty output, no error):
the name resolves via Pi-hole to the container's own address, and the
container firewall doesn't route that back. From outside, from the laptop or
phone, it works. The deploy isn't affected — its health check goes against
`http://127.0.0.1:8010/`, not against the HTTPS name.

The first run should report `nothing to do` if step 1 was carried out —
the repo is then already on the current commit. That's exactly the expected
result and proves the SHA comparison works.

## Enabling prod

Once the test container runs satisfactorily. The environment is pure
configuration — the script itself is identical in both containers.

**If the bootstrap above has already run, this is enough:**

```sh
printf 'GAMECOOLER_BRANCH=main\nGAMECOOLER_HOST=wildbret.home.arpa\n' \
  > /etc/default/gamecooler-autodeploy
systemctl enable --now gamecooler-autodeploy.timer
systemctl start gamecooler-autodeploy.service   # first run, right away
```

**If the bootstrap has never run** (the usual case if prod has so far been
updated by hand), the wrapper and units are missing — then go through the
**whole** bootstrap section above, with `main` instead of `dev` in step 1 and
step 2. Without the wrapper the timer fails with `status=203/EXEC`, because
`ExecStart` points to `/usr/local/bin/gamecooler-autodeploy`.

The first run is **not a no-op** if prod is on an older state: it rolls out
code, `gamecooler.service` and the Caddyfile. The Caddyfile is re-rendered in
the process — the name comes from `GAMECOOLER_HOST`, no longer from the file.

**Check beforehand:**

- Is `config.yaml` in prod set to `dry_run: false`, and does `backend: agent`
  point to the Mac? The deploy never touches `config.yaml`, but the app is
  restarted.
- Does the test container really have its **own** volume
  (`pct config <id> | grep mp0`), not prod's shared one?
- Is prod running right now? If the app is already sick, the run aborts
  before the deploy (`app not answering ...`) — fix that first.

## Operations

```sh
# Deploy immediately, without waiting for the timer
systemctl start gamecooler-autodeploy.service
journalctl -u gamecooler-autodeploy -n 40 --no-pager

# Next scheduled run
systemctl list-timers gamecooler-autodeploy.timer

# Pause the timer (e.g. while working on the container yourself)
systemctl stop gamecooler-autodeploy.timer
```

A run with nothing to do costs one `git ls-remote` and finishes in under a
second.

## Failure patterns

| Journal message | Meaning |
|---|---|
| `nothing to do — dev is at <sha>` | Normal, no deploy needed |
| `app not answering on ... — no deploy` | The app was already sick **before** the deploy. Find the cause, don't deploy. |
| `commit <sha> is marked as bad` | The rollback kicked in. Push a fix to the branch; the marker clears itself. |
| `Caddyfile invalid` | Syntax error in the repo Caddyfile. `caddy validate` rejected it, so Caddy was not reloaded and keeps running with the old configuration. |
| `GAMECOOLER_HOST missing in ...` / `exit 2` | This environment's name isn't set. Intentional: without it, a wrong name would be installed. |
| `Caddyfile still contains __HOST__` | The placeholder wasn't replaced — typo in the Caddyfile. **Nothing** was installed. |
| `OK — running <sha>, reachable as <host>` | Normal, not an error: the deploy succeeded, and the line shows which name Caddy now serves. |
| `deploy of <sha> failed — rolling back to <sha>` | The new commit didn't start (or a deploy step failed). The script rolls back; `rollback succeeded — running <sha> again` confirms it runs on the old state again. |
| `the rollback is not healthy either` | Serious case: both states sick. Intervene by hand. |
| `uv sync failed` | Usually a `uv.lock` that doesn't match the commit (`--locked` then aborts). Run `uv lock` locally and push again. |
| `fatal: $HOME not set` / `status=128` | systemd doesn't set `HOME` (no `User=` in the unit). The script sets it to `/root` itself — if the message still appears, the unit was installed from an old commit. |
| `fatal: detected dubious ownership` | `/opt/gamecooler` is owned by `gamecooler`, the script runs as root. The script sets `safe.directory` itself; when run by hand in a different repo, the exception is missing. |
| `could not write .../.autodeploy-bad` | The protection against the endless loop is gone — the timer rolls the same commit out and back again and again. Stop the timer immediately. |

**Delete the marker by hand** if a commit failed but is actually fine
(e.g. the fault was outside the repo):

```sh
rm /var/lib/gamecooler/.autodeploy-bad
```

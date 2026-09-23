#!/bin/bash
# Updates a gamecooler installation to the latest state of its branch. Runs
# as root in the container, started by the timer
# gamecooler-autodeploy.timer.
#
# The branch comes from /etc/default/gamecooler-autodeploy — the environment is
# pure configuration, this script is identical in all environments:
#
#   dev  -> test container
#   main -> prod
#
# Running it by hand (without waiting for the timer):
#
#   systemctl start gamecooler-autodeploy.service
#   journalctl -u gamecooler-autodeploy -n 40 --no-pager
#
# IMPORTANT: This script lives in the repo and gets overwritten by itself
# (step "fetch code"). That's why the timer doesn't call this file but the
# wrapper /usr/local/bin/gamecooler-autodeploy, which first copies it to a
# temporary file. Without that, bash keeps reading the script while it
# changes — bash reads by file offset and can bail out in the middle of a
# command. Don't "simplify" this by pointing the timer straight at this file.

set -euo pipefail

# systemd doesn't set HOME when the unit has no User= — the service then runs
# as root without HOME. That fails immediately:
#
#   fatal: $HOME not set      (exit 128, already on the first git command)
#
# Both tools needed here are affected: git config --global looks for
# ~/.gitconfig via HOME, and uv creates its cache under $HOME/.cache/uv.
# A single default takes care of both.
export HOME="${HOME:-/root}"

REPO=/opt/gamecooler
STATE=/var/lib/gamecooler
HEALTH_URL=http://127.0.0.1:8010/
BAD_MARKER="$STATE/.autodeploy-bad"
BRANCH_FILE=/etc/default/gamecooler-autodeploy

log() { echo "[autodeploy] $*"; }
die() { echo "[autodeploy] FEHLER: $*" >&2; exit 1; }

# --- Read configuration -----------------------------------------------------

BRANCH=dev
HOST=
if [ -r "$BRANCH_FILE" ]; then
	# shellcheck disable=SC1090
	. "$BRANCH_FILE"
	BRANCH="${GAMECOOLER_BRANCH:-dev}"
	HOST="${GAMECOOLER_HOST:-}"
fi

# Only the two branches that actually exist. A typo must not silently deploy
# some other branch.
case "$BRANCH" in
	dev | main) ;;
	*)
		echo "usage: GAMECOOLER_BRANCH muss 'dev' oder 'main' sein (ist: '$BRANCH')" >&2
		exit 2
		;;
esac

# No hostname, no deploy. There is deliberately NO default here: a silent
# fallback to the prod name has already once made the test container listen
# under the wrong name — reachable, but as wildbret.home.arpa, while
# gamecooler-test.home.arpa got no certificate. A missing value must fail more
# loudly than a wrong one.
if [ -z "$HOST" ]; then
	echo "usage: GAMECOOLER_HOST fehlt in $BRANCH_FILE" >&2
	echo "       z.B. GAMECOOLER_HOST=gamecooler-test.home.arpa" >&2
	exit 2
fi

# --- Health check as a function ---------------------------------------------

# $1 = attempts. After a restart the app needs a moment until uvicorn is
# listening; hence several attempts instead of one with a long timeout.
app_healthy() {
	local tries="${1:-10}" i
	for ((i = 1; i <= tries; i++)); do
		if curl -sf -o /dev/null --max-time 3 "$HEALTH_URL"; then
			return 0
		fi
		sleep 1
	done
	return 1
}

# Checks that Caddy answers under the name of THIS environment.
#
# The health check above goes to 127.0.0.1:8010 and never sees Caddy. That is
# exactly why it went unnoticed for a long time that the test container was
# listening under the prod name: the app was healthy, only the name was wrong.
#
# --resolve points to 127.0.0.1 instead of Pi-hole — so the name is checked
# directly against Caddy and not against DNS or the container firewall (which
# doesn't loop the same name back from inside, see docs/autodeploy.md).
#
# -k skips the trust check, but NOT the question of whether a certificate
# exists for this name at all: without a site the handshake aborts with
# "tlsv1 alert internal error". Exactly the error this is meant to catch.
site_healthy() {
	local tries="${1:-5}" i
	for ((i = 1; i <= tries; i++)); do
		if curl -sk -o /dev/null --max-time 3 \
			--resolve "$HOST:443:127.0.0.1" \
			"https://$HOST/" 2>/dev/null; then
			return 0
		fi
		sleep 1
	done
	return 1
}

# --- Render Caddyfile -------------------------------------------------------

# Substitutes this environment's name into deploy/Caddyfile and reloads Caddy.
#
# The name is substituted here, not read by Caddy from the environment. The
# Debian package's unit has no EnvironmentFile, so a {$VAR:default} in the
# Caddyfile always silently falls back to the default — that's how the test
# container listened under the prod name for a while: reachable, but as
# wildbret.home.arpa, while gamecooler-test.home.arpa got no certificate.
#
# A separate function because it is needed from two directions: during
# rollout and in the pre-check. The second case is the more important one — a
# container whose /etc/caddy/Caddyfile still carries the name of the previous
# installation is on the right commit, so it has nothing to do and would never
# be straightened out without this call.
apply_caddy() {
	[ -r "$REPO/deploy/Caddyfile" ] || {
		log "Caddyfile fehlt im Repo"
		return 1
	}

	# Render into a temporary file first, then install: if sed fails,
	# /etc/caddy/Caddyfile stays untouched instead of half-written.
	local rendered
	rendered=$(mktemp) || {
		log "mktemp fehlgeschlagen"
		return 1
	}
	if ! sed "s|__HOST__|$HOST|g" "$REPO/deploy/Caddyfile" >"$rendered"; then
		log "Caddyfile konnte nicht gerendert werden"
		rm -f "$rendered"
		return 1
	fi
	# Check that rendering really happened: a leftover __HOST__ or a typo in
	# the placeholder would otherwise pass as a site name.
	if grep -q '__HOST__' "$rendered"; then
		log "Caddyfile enthält noch __HOST__ — Platzhalter nicht ersetzt?"
		rm -f "$rendered"
		return 1
	fi
	if ! install -m644 "$rendered" /etc/caddy/Caddyfile; then
		log "Caddyfile konnte nicht installiert werden"
		rm -f "$rendered"
		return 1
	fi
	rm -f "$rendered"

	# validate before reload: a syntax error would otherwise kill Caddy on
	# reload, and you'd look for the fault in the certificate instead of the file.
	caddy validate --config /etc/caddy/Caddyfile || {
		log "Caddyfile ungültig"
		return 1
	}
	systemctl reload caddy || {
		log "Caddy-Reload fehlgeschlagen"
		return 1
	}
	return 0
}

cd "$REPO" || die "$REPO fehlt"

# This script runs as root, but /opt/gamecooler is owned by gamecooler (see
# docs/deploy.md phase 4). In this setup git refuses every command:
#
#   fatal: detected dubious ownership in repository at '/opt/gamecooler'
#
# The check has applied since CVE-2022-24765 and can't be turned off, only
# bypassed with an exception. The guard prevents another entry from piling up
# in /root/.gitconfig on every run.
if ! git config --global --get-all safe.directory 2>/dev/null | grep -qxF "$REPO"; then
	git config --global --add safe.directory "$REPO"
fi

# --- Determine the prior state ----------------------------------------------

PREV=$(git rev-parse HEAD)

# If the app is already broken, don't deploy. Otherwise the health check would
# fail after the update, the script would roll back to PREV — a state that is
# just as broken — and mask the actual fault. The marker is cleared as soon as
# a different commit shows up, so "broken and no deploy needed" is the only
# sensible outcome here.
if ! app_healthy 3; then
	log "App antwortet nicht auf $HEALTH_URL — kein Deploy. Erst reparieren."
	exit 1
fi

# The same for the configuration, and here rather than in one of the branches
# below: "code is current" and "correctly configured" are two separate
# questions. A container can be on the right commit, skip a commit marked as
# bad, or have nothing to do at all — and still listen under the wrong name.
# If the check lived only in the "nothing to do" branch, a container with the
# marker set would stay silent forever.
#
# No rollback and no marker on failure: the commit is fine, the configuration
# isn't. A rollback would render the same Caddyfile with the same name and go
# round in circles; a marker would block a good commit.
if ! site_healthy 3; then
	log "Caddy antwortet nicht auf https://$HOST/ — Caddyfile neu rendern"
	if apply_caddy && site_healthy 5; then
		log "geradegerückt — erreichbar als $HOST"
	else
		log "WARNUNG: https://$HOST/ bleibt unerreichbar."
		log "WARNUNG: prüfen: grep -n home.arpa /etc/caddy/Caddyfile"
		log "WARNUNG: und GAMECOOLER_HOST in $BRANCH_FILE"
		exit 1
	fi
fi

# --- Fetch the new state ----------------------------------------------------

# ls-remote returns only the SHA, without transferring objects. For a timer
# that runs every few minutes, that's the cheapest way to find out there's
# nothing to do.
#
# The || branch matters: with "set -e" a failed assignment would end the
# script immediately. A brief network outage would then look like a broken
# deploy, when GitHub was merely unreachable for a moment.
NEU=$(git ls-remote origin "refs/heads/$BRANCH" 2>/dev/null | cut -f1) || {
	log "origin nicht erreichbar — nächster Lauf versucht es erneut"
	exit 0
}
[ -n "$NEU" ] || die "Branch '$BRANCH' nicht auf origin gefunden"

if [ "$NEU" = "$PREV" ]; then
	log "nichts zu tun — $BRANCH ist auf $PREV"
	exit 0
fi

# A commit that has already been rolled back once is not retried. Without
# this marker the timer would run into an endless loop: deploy, fail, roll
# back, and the same again five minutes later.
if [ -f "$BAD_MARKER" ] && [ "$(cat "$BAD_MARKER")" = "$NEU" ]; then
	log "Commit $NEU ist als fehlerhaft markiert — übersprungen."
	log "Nach einem Fix auf $BRANCH verschwindet der Merker von selbst."
	exit 0
fi

log "Deploy $BRANCH: $PREV -> $NEU"

git fetch --depth=1 origin "$BRANCH" || die "git fetch fehlgeschlagen"
git checkout -q -B "$BRANCH" FETCH_HEAD || die "git checkout fehlgeschlagen"

# Applies the checked-out state: dependencies, Caddyfile, unit, restart.
#
# Deliberately a function with a return value rather than a sequence of
# commands with "|| die". If the sequence aborts halfway, the repo is already on
# the new commit while the service is still running the old code — and the next
# run sees NEU == PREV and reports "nichts zu tun". The half-rolled-out state
# would stay forever. As a function, every partial failure ends up in the same
# rollback as a failed health check.
#
# That's also why there's no "set -e" abort here: each step reports itself.
apply_release() {
	# --locked aborts if uv.lock doesn't match the code, instead of silently
	# re-resolving. A forgotten "uv lock" gets caught here.
	uv sync --locked --no-dev || {
		log "uv sync fehlgeschlagen"
		return 1
	}
	chown -R gamecooler:gamecooler "$REPO" || {
		log "chown fehlgeschlagen"
		return 1
	}

	if ! apply_caddy; then
		return 1
	fi

	install -m644 "$REPO/deploy/gamecooler.service" /etc/systemd/system/gamecooler.service || {
		log "gamecooler.service fehlt im Repo"
		return 1
	}
	systemctl daemon-reload || return 1
	systemctl restart gamecooler || {
		log "Neustart fehlgeschlagen"
		return 1
	}
	return 0
}

if apply_release && app_healthy 10; then
	# The name is checked separately, and a failure does NOT lead into the
	# rollback: the rollback renders the same Caddyfile with the same name, so
	# it can't fix a wrong configuration — it would go round in circles. The
	# commit is fine, the configuration isn't, and so the marker stays
	# unwritten: it would block a good commit.
	if ! site_healthy 5; then
		log "WARNUNG: die App läuft, aber Caddy antwortet nicht auf https://$HOST/"
		log "WARNUNG: erwartet wird ein Zertifikat für '$HOST' — kommt"
		log "WARNUNG: 'tlsv1 alert internal error', bedient Caddy einen anderen Namen."
		log "WARNUNG: prüfen: grep -n home.arpa /etc/caddy/Caddyfile"
		log "WARNUNG: und GAMECOOLER_HOST in $BRANCH_FILE"
		exit 1
	fi

	log "OK — läuft auf $NEU, erreichbar als $HOST"
	rm -f "$BAD_MARKER"
	exit 0
fi

# From here on nothing may abort hard: "set -e" would otherwise bail out in the
# middle of the recovery — the service would keep running the broken state and
# the marker would never be written, so the timer would retry the same commit
# endlessly.
log "Deploy von $NEU fehlgeschlagen — Rollback auf $PREV"

if git checkout -q --detach "$PREV"; then
	log "zurück auf $PREV"
else
	# Shouldn't happen with --depth=1 (PREV is still in the object store), but
	# if it does, a second attempt with a full fetch is cheaper than a
	# container stuck on a broken commit.
	log "WARNUNG: $PREV nicht im Objektspeicher — hole vollständig nach"
	git fetch --unshallow origin 2>/dev/null ||
		git fetch origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" ||
		log "WARNUNG: Nachladen fehlgeschlagen"
	git checkout -q --detach "$PREV" || log "WARNUNG: Rollback-Checkout fehlgeschlagen"
fi

# The same path as for rollout, just with the old state — so the Caddyfile is
# also back to its previous content in case apply_release only failed after
# that step.
apply_release || log "WARNUNG: Wiederherstellen des alten Stands unvollständig"

if app_healthy 10; then
	log "Rollback erfolgreich — läuft wieder auf $PREV"
else
	log "WARNUNG: auch der Rollback ist nicht gesund. Eingreifen nötig."
fi

# Write the marker only AFTER the rollback: if the script dies before that
# (power, OOM), the next run should be allowed to retry the commit.
#
# If writing fails, the protection against the endless loop is gone: the
# timer would roll out and roll back the same commit every five minutes. That
# has to be loud, otherwise you end up looking in the wrong place later.
mkdir -p "$STATE" 2>/dev/null || true
if ! echo "$NEU" >"$BAD_MARKER" 2>/dev/null; then
	log "WARNUNG: konnte $BAD_MARKER nicht schreiben!"
	log "WARNUNG: der fehlerhafte Commit wird beim nächsten Lauf ERNEUT versucht."
	log "WARNUNG: Timer stoppen, bis die Ursache behoben ist:"
	log "WARNUNG:   systemctl stop gamecooler-autodeploy.timer"
fi

# The timer should make the failure visible, hence no exit 0.
exit 1

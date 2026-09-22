#!/bin/bash
# Aktualisiert eine gamecooler-Installation auf den neuesten Stand ihres
# Branches. Läuft als root im Container, gestartet vom Timer
# gamecooler-autodeploy.timer.
#
# Der Branch kommt aus /etc/default/gamecooler-autodeploy — die Umgebung ist
# reine Konfiguration, dieses Skript ist in allen Umgebungen identisch:
#
#   dev  -> Test-Container
#   main -> Prod
#
# Aufruf von Hand (ohne auf den Timer zu warten):
#
#   systemctl start gamecooler-autodeploy.service
#   journalctl -u gamecooler-autodeploy -n 40 --no-pager
#
# WICHTIG: Dieses Skript liegt im Repo und wird von sich selbst überschrieben
# (Schritt "Code holen"). Deshalb ruft der Timer nicht diese Datei auf, sondern
# den Wrapper /usr/local/bin/gamecooler-autodeploy, der sie vorher in eine
# temporäre Datei kopiert. Ohne das liest bash das Skript weiter, während es
# sich ändert — bash liest über Datei-Offsets und kann mitten im Befehl
# aussteigen. Nicht "vereinfachen", indem der Timer direkt hierauf zeigt.

set -euo pipefail

# systemd setzt HOME nicht, wenn die Unit kein User= hat — dann läuft der
# Dienst als root ohne HOME. Das bricht sofort ab:
#
#   fatal: $HOME not set      (exit 128, schon beim ersten git-Kommando)
#
# Betroffen sind beide Werkzeuge, die hier gebraucht werden: git config
# --global sucht ~/.gitconfig über HOME, und uv legt seinen Cache unter
# $HOME/.cache/uv an. Ein einziger Default erschlägt beide.
export HOME="${HOME:-/root}"

REPO=/opt/gamecooler
STATE=/var/lib/gamecooler
HEALTH_URL=http://127.0.0.1:8010/
BAD_MARKER="$STATE/.autodeploy-bad"
BRANCH_FILE=/etc/default/gamecooler-autodeploy

log() { echo "[autodeploy] $*"; }
die() { echo "[autodeploy] FEHLER: $*" >&2; exit 1; }

# --- Konfiguration lesen ----------------------------------------------------

BRANCH=dev
HOST=
if [ -r "$BRANCH_FILE" ]; then
	# shellcheck disable=SC1090
	. "$BRANCH_FILE"
	BRANCH="${GAMECOOLER_BRANCH:-dev}"
	HOST="${GAMECOOLER_HOST:-}"
fi

# Nur die beiden Branches, die es wirklich gibt. Ein Tippfehler soll nicht
# stillschweigend auf irgendeinen anderen Branch deployen.
case "$BRANCH" in
	dev | main) ;;
	*)
		echo "usage: GAMECOOLER_BRANCH muss 'dev' oder 'main' sein (ist: '$BRANCH')" >&2
		exit 2
		;;
esac

# Ohne Hostname wird nicht deployt. Hier ist bewusst KEIN Default: ein
# stillschweigender Rückfall auf den Prod-Namen hat den Test-Container schon
# einmal unter falschem Namen lauschen lassen — erreichbar, aber unter
# wildbret.home.arpa, während gamecooler-test.home.arpa kein Zertifikat bekam.
# Ein fehlender Wert muss lauter scheitern als ein falscher.
if [ -z "$HOST" ]; then
	echo "usage: GAMECOOLER_HOST fehlt in $BRANCH_FILE" >&2
	echo "       z.B. GAMECOOLER_HOST=gamecooler-test.home.arpa" >&2
	exit 2
fi

# --- Health-Check als Funktion ----------------------------------------------

# $1 = Versuche. Die App braucht nach dem Neustart einen Moment, bis uvicorn
# lauscht; deshalb mehrfach versuchen statt einmal mit langem Timeout.
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

# Prüft, dass Caddy unter dem Namen DIESER Umgebung antwortet.
#
# Der Health-Check oben geht gegen 127.0.0.1:8010 und sieht Caddy nie. Genau
# deshalb blieb lange unbemerkt, dass der Test-Container unter dem Prod-Namen
# lauschte: die App war gesund, nur der Name stimmte nicht.
#
# --resolve zeigt auf 127.0.0.1 statt auf Pi-hole — der Name wird also direkt
# gegen Caddy geprüft und nicht gegen DNS oder die Container-Firewall (die
# denselben Namen von innen nicht zurückleitet, siehe docs/autodeploy.md).
#
# -k überspringt die Vertrauensprüfung, aber NICHT die Frage, ob es für diesen
# Namen überhaupt ein Zertifikat gibt: ohne Site bricht der Handshake mit
# "tlsv1 alert internal error" ab. Genau der Fehler, den es zu fangen gilt.
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

# --- Caddyfile rendern ------------------------------------------------------

# Setzt den Namen dieser Umgebung in deploy/Caddyfile ein und lädt Caddy neu.
#
# Der Name wird hier eingesetzt, nicht von Caddy aus der Umgebung gelesen. Die
# Unit des Debian-Pakets hat kein EnvironmentFile, ein {$VAR:default} im
# Caddyfile fällt also immer still auf den Default zurück — so hat der
# Test-Container eine Zeitlang unter dem Prod-Namen gelauscht: erreichbar, aber
# unter wildbret.home.arpa, während gamecooler-test.home.arpa kein Zertifikat
# bekam.
#
# Eigene Funktion, weil sie aus zwei Richtungen gebraucht wird: beim Ausrollen
# und in der Vorabprüfung. Der zweite Fall ist der wichtigere — ein Container,
# dessen /etc/caddy/Caddyfile noch den Namen der Vorgänger-Installation trägt,
# steht auf dem richtigen Commit, hat also nichts zu tun und würde ohne diesen
# Aufruf nie wieder geradegerückt.
apply_caddy() {
	[ -r "$REPO/deploy/Caddyfile" ] || {
		log "Caddyfile fehlt im Repo"
		return 1
	}

	# Erst in eine temporäre Datei rendern, dann installieren: schlägt sed
	# fehl, bleibt /etc/caddy/Caddyfile unangetastet statt halb geschrieben.
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
	# Kontrolle, dass wirklich gerendert wurde: ein vergessenes __HOST__ oder
	# ein Tippfehler im Platzhalter würde sonst als Site-Name durchgehen.
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

	# validate vor dem Reload: ein Syntaxfehler würde Caddy sonst beim Neuladen
	# sterben lassen, und man sucht den Fehler im Zertifikat statt in der Datei.
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

# Dieses Skript läuft als root, /opt/gamecooler gehört aber gamecooler (siehe
# docs/deploy.md Phase 4). Git verweigert in dieser Konstellation jeden Befehl:
#
#   fatal: detected dubious ownership in repository at '/opt/gamecooler'
#
# Die Prüfung gilt seit CVE-2022-24765 und lässt sich nicht abschalten, nur
# per Ausnahme. Der Guard verhindert, dass sich bei jedem Lauf ein weiterer
# Eintrag in /root/.gitconfig ansammelt.
if ! git config --global --get-all safe.directory 2>/dev/null | grep -qxF "$REPO"; then
	git config --global --add safe.directory "$REPO"
fi

# --- Zustand vorher feststellen ---------------------------------------------

PREV=$(git rev-parse HEAD)

# Ist die App schon kaputt, wird nicht deployt. Sonst würde der Health-Check
# nach dem Update fehlschlagen, das Skript auf PREV zurückrollen — also auf
# einen Stand, der genauso kaputt ist — und den eigentlichen Fehler verdecken.
# Der Merker wird gelöscht, sobald ein anderer Commit auftaucht, deshalb ist
# "kaputt und kein Deploy nötig" hier der einzig sinnvolle Ausgang.
if ! app_healthy 3; then
	log "App antwortet nicht auf $HEALTH_URL — kein Deploy. Erst reparieren."
	exit 1
fi

# Dasselbe für die Konfiguration, und zwar hier statt in einem der Zweige
# unten: "Code ist aktuell" und "richtig konfiguriert" sind zwei Fragen. Ein
# Container kann auf dem richtigen Commit stehen, einen als fehlerhaft
# markierten Commit überspringen oder gar nichts zu tun haben — und trotzdem
# unter dem falschen Namen lauschen. Stünde die Prüfung nur im
# "nichts zu tun"-Zweig, bliebe ein Container mit gesetztem Merker für immer
# stumm.
#
# Kein Rollback und kein Merker bei Fehlschlag: der Commit ist in Ordnung, die
# Konfiguration ist es nicht. Ein Rollback würde dasselbe Caddyfile mit
# demselben Namen rendern und liefe im Kreis; ein Merker würde einen guten
# Commit sperren.
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

# --- Neuen Stand holen ------------------------------------------------------

# ls-remote liefert nur den SHA, ohne Objekte zu übertragen. Bei einem Timer,
# der alle paar Minuten läuft, ist das der billigste Weg festzustellen, dass
# nichts zu tun ist.
#
# Der ||-Zweig ist wichtig: mit "set -e" würde eine gescheiterte Zuweisung das
# Skript sofort beenden. Ein kurzer Netzausfall sähe dann wie ein kaputter
# Deploy aus, obwohl nur GitHub gerade nicht erreichbar war.
NEU=$(git ls-remote origin "refs/heads/$BRANCH" 2>/dev/null | cut -f1) || {
	log "origin nicht erreichbar — nächster Lauf versucht es erneut"
	exit 0
}
[ -n "$NEU" ] || die "Branch '$BRANCH' nicht auf origin gefunden"

if [ "$NEU" = "$PREV" ]; then
	log "nichts zu tun — $BRANCH ist auf $PREV"
	exit 0
fi

# Ein Commit, der schon einmal zurückgerollt wurde, wird nicht erneut
# versucht. Ohne diesen Merker liefe der Timer in eine Endlosschleife:
# deployen, scheitern, zurückrollen, fünf Minuten später dasselbe.
if [ -f "$BAD_MARKER" ] && [ "$(cat "$BAD_MARKER")" = "$NEU" ]; then
	log "Commit $NEU ist als fehlerhaft markiert — übersprungen."
	log "Nach einem Fix auf $BRANCH verschwindet der Merker von selbst."
	exit 0
fi

log "Deploy $BRANCH: $PREV -> $NEU"

git fetch --depth=1 origin "$BRANCH" || die "git fetch fehlgeschlagen"
git checkout -q -B "$BRANCH" FETCH_HEAD || die "git checkout fehlgeschlagen"

# Übernimmt den ausgecheckten Stand: Abhängigkeiten, Caddyfile, Unit, Neustart.
#
# Bewusst eine Funktion mit Rückgabewert statt einer Folge von Befehlen mit
# "|| die". Bricht die Folge in der Mitte ab, steht das Repo schon auf dem neuen
# Commit, während der Dienst noch den alten Code fährt — und der nächste Lauf
# sieht NEU == PREV und meldet "nichts zu tun". Der halb ausgerollte Zustand
# bliebe für immer stehen. Als Funktion landet jeder Teilfehler im selben
# Rollback wie ein fehlgeschlagener Health-Check.
#
# Deshalb hier auch kein "set -e"-Abbruch: jeder Schritt meldet selbst.
apply_release() {
	# --locked bricht ab, wenn uv.lock nicht zum Stand passt, statt still
	# aufzulösen. Ein vergessenes "uv lock" fällt damit hier auf.
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
	# Der Name wird separat geprüft, und ein Fehlschlag führt NICHT in den
	# Rollback: der Rollback rendert dasselbe Caddyfile mit demselben Namen,
	# kann eine falsche Konfiguration also nicht reparieren — er liefe im
	# Kreis. Der Commit ist in Ordnung, die Konfiguration ist es nicht, und der
	# Merker bleibt deshalb ungeschrieben: er würde einen guten Commit sperren.
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

# Ab hier darf nichts mehr hart abbrechen: "set -e" würde sonst mitten in der
# Wiederherstellung aussteigen — der Dienst liefe mit dem kaputten Stand weiter
# und der Merker würde nie geschrieben, also versuchte der Timer denselben
# Commit endlos.
log "Deploy von $NEU fehlgeschlagen — Rollback auf $PREV"

if git checkout -q --detach "$PREV"; then
	log "zurück auf $PREV"
else
	# Sollte mit --depth=1 nicht vorkommen (PREV liegt noch im Objektspeicher),
	# aber wenn doch, ist ein zweiter Versuch mit vollem Fetch billiger als ein
	# Container, der auf einem kaputten Commit stehen bleibt.
	log "WARNUNG: $PREV nicht im Objektspeicher — hole vollständig nach"
	git fetch --unshallow origin 2>/dev/null ||
		git fetch origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" ||
		log "WARNUNG: Nachladen fehlgeschlagen"
	git checkout -q --detach "$PREV" || log "WARNUNG: Rollback-Checkout fehlgeschlagen"
fi

# Derselbe Weg wie beim Ausrollen, nur mit dem alten Stand — damit steht auch
# das Caddyfile wieder auf dem vorherigen Inhalt, falls apply_release erst
# danach gescheitert ist.
apply_release || log "WARNUNG: Wiederherstellen des alten Stands unvollständig"

if app_healthy 10; then
	log "Rollback erfolgreich — läuft wieder auf $PREV"
else
	log "WARNUNG: auch der Rollback ist nicht gesund. Eingreifen nötig."
fi

# Merker erst NACH dem Rollback schreiben: bricht das Skript vorher ab (Strom,
# OOM), soll der nächste Lauf den Commit erneut versuchen dürfen.
#
# Schlägt das Schreiben fehl, ist der Schutz gegen die Endlosschleife weg: der
# Timer würde denselben Commit alle fünf Minuten erneut ausrollen und
# zurückrollen. Das muss laut sein, sonst sucht man später im falschen Eck.
mkdir -p "$STATE" 2>/dev/null || true
if ! echo "$NEU" >"$BAD_MARKER" 2>/dev/null; then
	log "WARNUNG: konnte $BAD_MARKER nicht schreiben!"
	log "WARNUNG: der fehlerhafte Commit wird beim nächsten Lauf ERNEUT versucht."
	log "WARNUNG: Timer stoppen, bis die Ursache behoben ist:"
	log "WARNUNG:   systemctl stop gamecooler-autodeploy.timer"
fi

# Der Timer soll den Fehlschlag sichtbar machen, deshalb kein exit 0.
exit 1

#!/usr/bin/env bash
# sim-host image minimalism / spec-compliance checks (sim-network.md §7/§8).
#
# Guards against the built image diverging from its intended minimal state —
# the class of regression the Alpine base image can silently re-introduce:
#   - inherited Incus templates that regenerate /etc files at instance create
#     (overwriting the build's no-gettys inittab);
#   - extra OpenRC services enabled by the base (syslog — forbidden by §7;
#     crond, networking — beyond the §8 minimal set);
#   - extra packages the base drags in (doas, logrotate, busybox-suid, … —
#     "fewer tools available to an attacker", §8).
#
# Inspects a throwaway instance launched from the built image. Requires the
# image (make images) and incus; needs no full deployment. Run from deploy/:
#   bash tests/integration/test_sim_image.sh

set -uo pipefail

INCUS="${INCUS:-incus}"
IMAGE="${IMAGE:-ravensport/sim:latest}"
CTR="sim-image-test-$$"
PASS=0
FAIL=0

# ── reporting + helpers ──────────────────────────────────────────────────────
pass()    { printf "  PASS  %s\n" "$1"; PASS=$((PASS + 1)); }
fail()    { printf "  FAIL  %s\n" "$1"; FAIL=$((FAIL + 1)); }
section() { echo; echo "--- $1 ---"; }
# die aborts the run rather than printing a (potentially vacuous) PASS: every
# check reads the instance via `incus exec`, so an unready or empty read must
# fail loudly, never collapse to a green.
die()     { echo "ERROR: $1" >&2; exit 1; }

in_ctr()  { $INCUS exec "$CTR" -- "$@"; }                  # run a command in the image
present() { if in_ctr test -e "$1"; then pass "present: $1"; else fail "missing: $1"; fi; }
absent()  { if in_ctr test -e "$1"; then fail "leaked into image: $1"; else pass "absent: $1"; fi; }

cleanup() { $INCUS delete -f "$CTR" 2>/dev/null || true; }
trap cleanup EXIT

# ── launch a throwaway instance and confirm it is inspectable ────────────────
$INCUS image info "$IMAGE" >/dev/null 2>&1 \
    || { echo "image '$IMAGE' not found — run 'make images' first" >&2; exit 2; }

echo "==> sim-host image checks ($IMAGE)"

$INCUS launch "$IMAGE" "$CTR" >/dev/null 2>&1 \
    || die "incus launch of $IMAGE failed (image corrupt, or storage/profile issue)"
ready=
for _ in $(seq 1 30); do
    in_ctr true 2>/dev/null && { ready=1; break; }
    sleep 1
done
[ -n "$ready" ] || die "$CTR did not become exec-ready; cannot inspect the image"

# ── snapshot the image state once (enabled services, installed packages) ─────
# A throwaway instance reflects a deployed sim-host: create-time templates have
# fired, services are enabled, packages installed. The image has no apk binary,
# so packages are read from the apk database. An empty capture means the read
# failed (not a clean image), so guard against a vacuous green.
# shellcheck disable=SC2016  # $d must reach the container's shell unexpanded
enabled=$(in_ctr sh -c 'for d in /etc/runlevels/*; do ls "$d"; done' 2>/dev/null)
pkgs=$(in_ctr sh -c 'grep "^P:" /lib/apk/db/installed 2>/dev/null | cut -d: -f2')
[ -n "$enabled" ] || die "read no enabled services from $CTR (OpenRC runlevels empty?)"
[ -n "$pkgs" ]    || die "read no packages from $CTR (apk database unreadable?)"

# ── checks ───────────────────────────────────────────────────────────────────
section "No inherited Incus templates (else they overwrite build files at create)"
# Do not swallow stderr: an unavailable subcommand must error, not report zero.
templates=$($INCUS config template list "$CTR" --format csv) || die "could not list templates on $CTR"
if [ -z "$templates" ]; then
    pass "no inherited templates"
else
    fail "$(printf '%s' "$templates" | grep -c .) template(s) present"
fi

section "Init: OpenRC runlevels, no gettys (§7/§8)"
# Check inittab exists first: a bare grep would report 'no getty' for a missing
# file, masking the regression this guards against.
if ! in_ctr test -f /etc/inittab; then
    fail "/etc/inittab is missing"
elif in_ctr grep -q getty /etc/inittab; then
    fail "/etc/inittab spawns a getty"
else
    pass "/etc/inittab has no getty"
fi
default_runlevel=$(in_ctr sh -c 'ls /etc/runlevels/default 2>/dev/null | sort | tr "\n" " "')
if [ "$default_runlevel" = "haproxy sim sim-net " ]; then
    pass "default runlevel = {sim-net, sim, haproxy}"
else
    fail "default runlevel = [$default_runlevel] (expected [haproxy sim sim-net ])"
fi

section "Forbidden daemons not enabled (§7 no syslog; §8 no remote-access)"
for svc in syslog crond networking sshd dropbear; do
    if printf '%s\n' "$enabled" | grep -qx "$svc"; then
        fail "service '$svc' enabled"
    else
        pass "service '$svc' not enabled"
    fi
done

section "Package set matches the minimal manifest (§8)"
# The manifest is the deterministic closure of the §8 seed; any drift (a base
# extra, or a new dependency from a version bump) fails here and must be
# reconciled deliberately. Regenerate after a seed change: run build-sim-image.sh's
# `apk --root … add "$SIM_PKGS"`, then `grep '^P:' <root>/lib/apk/db/installed |
# cut -d: -f2 | sort` into sim-image.expected.packages.
manifest="$(dirname "$0")/sim-image.expected.packages"
if [ ! -r "$manifest" ]; then
    fail "manifest not found: $manifest"
else
    added=$(comm -13 <(sort -u "$manifest") <(printf '%s\n' "$pkgs" | sort -u))
    removed=$(comm -23 <(sort -u "$manifest") <(printf '%s\n' "$pkgs" | sort -u))
    if [ -z "$added$removed" ]; then
        pass "package set matches manifest ($(grep -c . "$manifest") packages)"
    else
        [ -n "$added" ]   && fail "unexpected packages: ${added//$'\n'/ }"
        [ -n "$removed" ] && fail "missing packages: ${removed//$'\n'/ }"
    fi
fi

section "Required artefacts present, build state absent"
for f in /usr/local/bin/sim /usr/local/bin/sim-net.sh \
         /etc/haproxy/haproxy.cfg /etc/nftables/egress-guard.nft \
         /etc/init.d/sim-net /etc/init.d/sim /etc/init.d/haproxy; do
    present "$f"
done
for d in /in /sim-host /etc/apk; do      # build inputs and apk state must not be baked in
    absent "$d"
done
# Services run unprivileged via OpenRC command_user (§8); assert the accounts
# exist, else a silently-failed adduser would leave them running as root.
for u in sim haproxy; do
    if in_ctr grep -q "^$u:" /etc/passwd; then
        pass "service user present: $u"
    else
        fail "service user missing: $u (would run as root)"
    fi
done

section "No setuid binaries (§8 hardening)"
# Services run unprivileged and HAProxy holds CAP_NET_BIND_SERVICE, so no setuid
# binary is needed. Guard the capture: an exec failure must error, not go green
# (find legitimately returns nothing when there are none).
suid=$(in_ctr sh -c 'find / -xdev -type f -perm -4000 2>/dev/null') || die "setuid scan failed on $CTR"
suid=${suid//$'\n'/ }
if [ -z "${suid// /}" ]; then
    pass "no setuid binaries"
else
    fail "setuid binaries present: $suid"
fi

echo
echo "==> Results: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]

#!/usr/bin/env bash
# Deploys the example Track A entry with Packetbeat (README.md, "Deployment").
#
# Usage:
#   deploy-entry.sh check
#       Checks that dist/ holds Packetbeat's binary for this machine's
#       architecture, with the digest that packetbeat.sha256 records. The
#       binary comes from build-packetbeat.sh.
#   deploy-entry.sh override
#       Writes the entry's own version of the competition's additional
#       override file to dist/override.yaml, from the competition's file and
#       the entry's change, override-patch.yaml (override.py).
#   deploy-entry.sh images
#       Writes the override file, then builds the images of a living port of
#       the entry in deploy/ with it: the entry's image of siem-01,
#       ravensport/siem-01-packetbeat:latest, and every other node's
#       competition or operational image. PROFILE is passed to make. The
#       attack chains' binaries must be staged first, with make stage-attacks
#       in deploy/.
#   deploy-entry.sh volume DIRECTORY
#       Writes the entry's storage volume to DIRECTORY, which must be empty or
#       absent: the defender, the shared step protocol beside it, and the
#       telemetry specification. The episode runner attaches the volume to
#       siem-01 at /var/lib/entry.
#
# The episode runner and the living-port manager take dist/override.yaml with
# --extra-override, after deploy-entry.sh override or images has written it.
# PYTHON names the interpreter for override.py, which needs PyYAML; by default
# it is the virtual environment's in deploy/, or else python3.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../../.." && pwd)
binary_name() { echo "packetbeat-oss-9.5.5-linux-$1"; }

architecture() {
    case "$(uname -m)" in
        x86_64) echo amd64 ;;
        aarch64 | arm64) echo arm64 ;;
        *)
            echo "ERROR: unsupported architecture $(uname -m)" >&2
            exit 1
            ;;
    esac
}

check() {
    local name
    name=$(binary_name "$(architecture)")
    if [ ! -f "$here/dist/$name" ]; then
        echo "ERROR: dist/$name is missing; build it with build-packetbeat.sh" >&2
        exit 1
    fi
    (cd "$here/dist" && grep " $name\$" ../packetbeat.sha256 | sha256sum -c -)
}

override() {
    local python=${PYTHON:-"$repo/deploy/.venv-$(uname -s)-$(uname -m)/bin/python"}
    [ -x "$python" ] || python=python3
    "$python" "$here/override.py" "$here/dist/override.yaml"
}

images() {
    check
    override
    make -C "$repo/deploy" build EXTRA_OVERRIDE="$here/dist/override.yaml" \
        ${PROFILE:+PROFILE="$PROFILE"}
}

volume() {
    local target=$1 protocol="$here/../common/protocol.py"
    if [ ! -f "$protocol" ]; then
        echo "ERROR: $protocol is missing" >&2
        exit 1
    fi
    if [ -e "$target" ] && [ -n "$(ls -A "$target")" ]; then
        echo "ERROR: $target is not empty" >&2
        exit 1
    fi
    mkdir -p "$target"
    install -m 0755 "$here/defender" "$target/defender"
    install -m 0644 "$protocol" "$target/protocol.py"
    install -m 0644 "$here/telemetry.json" "$target/telemetry.json"
}

case "${1:-}" in
    check) check ;;
    override) override ;;
    images) images ;;
    volume) volume "${2:?usage: deploy-entry.sh volume DIRECTORY}" ;;
    *)
        echo "usage: deploy-entry.sh check | override | images | volume DIRECTORY" >&2
        exit 2
        ;;
esac

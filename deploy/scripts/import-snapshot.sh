#!/usr/bin/env bash
# Import a snapshot file as a local snapshot set (deployment.md §10.4). The
# file is checked before anything is changed: its format version and
# architecture, every member's digest, the absence of RavensPort instances, and
# the storage pool's free space. The networks are then created from the file's
# own rendered commands, the instances imported and snapshotted, and the
# checkpoint, stop time, rendered files and inputs copied to
# snapshots/<label>/. Nothing is rendered and no image is built, so the
# local world.yaml, topology and profile are not read.
#
# Usage: import-snapshot.sh <snapshot file>
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/snapshot-lib.sh
source scripts/snapshot-lib.sh

FILE="${1:-}"
test -f "$FILE" || die "usage: import-snapshot.sh <snapshot file>"

MANIFEST=$(tar -xOf "$FILE" manifest.json 2>/dev/null) ||
    die "$FILE is not a snapshot file: it has no manifest.json"
field() {
    jq -r "$1" <<<"$MANIFEST"
}
archive_of() {
    field ".instances[] | select(.name == \"$1\") | .archive"
}

# ── checks, before anything is changed ────────────────────────────────────────

[ "$(field .format_version)" = "$SNAPSHOT_FORMAT_VERSION" ] ||
    die "$FILE has format version $(field .format_version); this deployment reads format version $SNAPSHOT_FORMAT_VERSION"
[ "$(field .arch)" = "$(uname -m)" ] ||
    die "$FILE was produced on $(field .arch); this host is $(uname -m)"
LABEL=$(field .label)
[[ $LABEL =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "$FILE names an invalid label '$LABEL'"
mapfile -t INSTANCES < <(field '.instances[].name')
for instance in "${INSTANCES[@]}"; do
    if ! [[ $instance =~ ^[a-z0-9][a-z0-9-]*$ ]] ||
        [ "$(archive_of "$instance")" != "instances/$instance.tar.zst" ]; then
        die "$FILE lists an invalid instance entry '$instance'"
    fi
done
mapfile -t FILES < <(field '.files[].path')
for member in "${FILES[@]}"; do
    if ! [[ $member =~ ^(rendered|inputs)(/[A-Za-z0-9_][A-Za-z0-9._-]*)+$ ]]; then
        die "$FILE lists an invalid file entry '$member'"
    fi
done
for file in "${RENDERED_FILES[@]}"; do
    printf '%s\n' "${FILES[@]}" | grep -qx "rendered/$file" ||
        die "$FILE lacks rendered/$file"
done

for instance in "${INSTANCES[@]}"; do
    if instance_exists "$instance"; then
        die "$instance already exists; remove the RavensPort instances first with make -C deploy down" \
            "(with FORCE=1 if they hold snapshot sets, which it then deletes)"
    fi
done

required=$(field '[.instances[].disk_usage_bytes] | add')
available=$("$INCUS" query /1.0/storage-pools/default/resources | jq '.space.total - .space.used')
[ "$available" -ge "$required" ] ||
    die "the storage pool has $available bytes free; the instances use $required"

SET_DIR="$SNAPSHOTS_DIR/$LABEL"
STAGING="$SNAPSHOTS_DIR/.import-$$"
trap 'rm -rf "$STAGING"' EXIT
mkdir -p "$STAGING"
echo "==> Checking $FILE"
tar -xf "$FILE" -C "$STAGING"

check_digest() {
    local member=$1 expected=$2
    [ "$(sha256sum "$STAGING/$member" 2>/dev/null | cut -d' ' -f1)" = "$expected" ] ||
        die "$member in $FILE does not match its digest: the file is truncated or corrupted"
}
check_digest checkpoint.json "$(field .checkpoint_sha256)"
check_digest stopped-at "$(field .stopped_at_sha256)"
for instance in "${INSTANCES[@]}"; do
    check_digest "$(archive_of "$instance")" \
        "$(field ".instances[] | select(.name == \"$instance\") | .sha256")"
done
for member in "${FILES[@]}"; do
    check_digest "$member" "$(field ".files[] | select(.path == \"$member\") | .sha256")"
done
[ "$(jq -r '.nodes[]' "$STAGING/rendered/manifest.json" | sort)" = "$(printf '%s\n' "${INSTANCES[@]}" | sort)" ] ||
    die "$FILE lists instances other than those its rendered manifest names"

if [ -e "$SET_DIR/checkpoint.json" ] && ! cmp -s "$SET_DIR/checkpoint.json" "$STAGING/checkpoint.json"; then
    die "$SET_DIR holds a different set with the label '$LABEL'"
fi

commit=$(git rev-parse HEAD 2>/dev/null || echo unknown)
if [ "$(field .commit)" != "$commit" ]; then
    echo "WARNING: $FILE was produced from commit $(field .commit); this checkout is $commit." >&2
    echo "         The instances and networks come from the file; only the snapshot scripts come from this checkout." >&2
fi

# ── import ─────────────────────────────────────────────────────────────────────

echo "==> Creating the zone networks"
# The file's rendered commands up to the instances: the pre-flight checks and
# the networks, without the instances, which come from the file's exports.
bash <(sed '/^# --- Instances ---$/,$d' "$STAGING/rendered/incus-commands.sh")

# From here the deployed instances are the set's (snapshot-lib.sh).
mkdir -p "$SNAPSHOTS_DIR"
echo "$LABEL" >"$DEPLOYED_SET"
for instance in "${INSTANCES[@]}"; do
    echo "==> Importing $instance"
    "$INCUS" import "$STAGING/instances/$instance.tar.zst" "$instance"
    for network in $("$INCUS" query "/1.0/instances/$instance" |
        jq -r '.expanded_devices[] | select(.type == "nic") | .network // .parent'); do
        "$INCUS" query "/1.0/networks/$network" >/dev/null 2>&1 ||
            die "$instance is attached to the network $network, which this topology does not create"
    done
done

mkdir -p "$SET_DIR"
cp "$STAGING/checkpoint.json" "$STAGING/stopped-at" "$SET_DIR/"
rm -rf "$SET_DIR/rendered" "$SET_DIR/inputs"
cp -R "$STAGING/rendered" "$STAGING/inputs" "$SET_DIR/"
for instance in "${INSTANCES[@]}"; do
    "$INCUS" snapshot create "$instance" "$LABEL"
done

echo "==> Imported the snapshot set '$LABEL'"

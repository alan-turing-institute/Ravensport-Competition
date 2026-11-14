#!/usr/bin/env bash
# Write a local snapshot set to one snapshot file (deployment.md §10.3):
# snapshots/ravensport-snapshot-<label>-<profile>-<arch>.tar, holding a
# manifest, each instance's export, the sim's checkpoint and the stop time, and
# the set's rendered files and the inputs they were rendered from.
# Each instance is exported from its snapshot, not its current state, through a
# temporary copy.
#
# Usage: export-snapshot.sh <label>
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/snapshot-lib.sh
source scripts/snapshot-lib.sh

LABEL="${1:-}"
[ -n "$LABEL" ] || die "usage: make -C deploy export-snapshot LABEL=<label>"
require_complete_set "$LABEL"
SET_DIR="$SNAPSHOTS_DIR/$LABEL"
for file in checkpoint.json stopped-at "${RENDERED_FILES[@]/#/rendered/}"; do
    test -f "$SET_DIR/$file" || die "$SET_DIR/$file not found: '$LABEL' is not a snapshot set"
done
test -d "$SET_DIR/inputs" || die "$SET_DIR/inputs not found: '$LABEL' is not a snapshot set"

INSTANCES=$(jq -r '.nodes[]' "$SET_DIR/rendered/manifest.json")
for instance in $INSTANCES; do
    "$INCUS" query "/1.0/instances/$instance/snapshots/$LABEL" >/dev/null 2>&1 ||
        die "$instance has no snapshot '$LABEL'"
done

PROFILE_NAME=$(jq -r .profile "$SET_DIR/rendered/manifest.json")
ARCH=$(uname -m)
OUTPUT=$(snapshot_file "$LABEL")
STAGING="$SNAPSHOTS_DIR/.export-$LABEL"
TEMPORARY_PREFIX="snapshot-export-"

cleanup() {
    local instance
    for instance in $INSTANCES; do
        "$INCUS" delete --force "$TEMPORARY_PREFIX$instance" >/dev/null 2>&1 || true
    done
    rm -rf "$STAGING"
}
trap cleanup EXIT
rm -rf "$STAGING"
mkdir -p "$STAGING/instances"

digest() {
    sha256sum "$1" | cut -d' ' -f1
}

entries='[]'
for instance in $INSTANCES; do
    temporary="$TEMPORARY_PREFIX$instance"
    archive="instances/$instance.tar.zst"
    echo "==> Exporting $instance from '$LABEL'"
    "$INCUS" copy "$instance/$LABEL" "$temporary"
    # A copy is given new MAC addresses; the export carries the snapshot's,
    # so that the restored hosts keep the producer's.
    mapfile -t hwaddrs < <("$INCUS" query "/1.0/instances/$instance/snapshots/$LABEL" |
        jq -r '.config | to_entries[] | select(.key | test("^volatile\\..+\\.hwaddr$"))
            | "\(.key)=\(.value)"')
    [ "${#hwaddrs[@]}" -eq 0 ] || "$INCUS" config set "$temporary" "${hwaddrs[@]}"
    "$INCUS" export "$temporary" "$STAGING/$archive" --instance-only --compression zstd
    "$INCUS" delete "$temporary"
    # The disk usage is the size of the uncompressed export, which an import
    # writes back to the storage pool. Incus reports no usage for an instance
    # on a btrfs pool without quota groups.
    usage=$(zstd -dc "$STAGING/$archive" | wc -c)
    entries=$(jq --arg name "$instance" --arg archive "$archive" \
        --arg sha256 "$(digest "$STAGING/$archive")" --argjson usage "$usage" \
        '. + [{name: $name, archive: $archive, sha256: $sha256, disk_usage_bytes: $usage}]' \
        <<<"$entries")
done

cp "$SET_DIR/checkpoint.json" "$SET_DIR/stopped-at" "$STAGING/"
cp -R "$SET_DIR/rendered" "$SET_DIR/inputs" "$STAGING/"

files='[]'
while IFS= read -r member; do
    files=$(jq --arg path "$member" --arg sha256 "$(digest "$STAGING/$member")" \
        '. + [{path: $path, sha256: $sha256}]' <<<"$files")
done < <(cd "$STAGING" && find rendered inputs -type f | sort)

jq -n \
    --argjson format_version "$SNAPSHOT_FORMAT_VERSION" \
    --arg set_label "$LABEL" \
    --arg profile "$PROFILE_NAME" \
    --arg arch "$ARCH" \
    --arg commit "$(git rev-parse HEAD)" \
    --arg incus_version "$("$INCUS" query /1.0 | jq -r .environment.server_version)" \
    --arg stopped_at "$(cat "$SET_DIR/stopped-at")" \
    --arg checkpoint_sha256 "$(digest "$STAGING/checkpoint.json")" \
    --arg stopped_at_sha256 "$(digest "$STAGING/stopped-at")" \
    --argjson instances "$entries" \
    --argjson files "$files" \
    '{format_version: $format_version, "label": $set_label, profile: $profile, arch: $arch,
      commit: $commit, incus_version: $incus_version, stopped_at: $stopped_at,
      instances: $instances, files: $files,
      checkpoint_sha256: $checkpoint_sha256, stopped_at_sha256: $stopped_at_sha256}' \
    >"$STAGING/manifest.json"

tar -cf "$OUTPUT.tmp" -C "$STAGING" manifest.json instances checkpoint.json stopped-at rendered inputs
mv "$OUTPUT.tmp" "$OUTPUT"

echo
echo "Snapshot file written: $OUTPUT"

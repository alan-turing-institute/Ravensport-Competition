#!/usr/bin/env bash
# Runs every rendered per-node build script (deployment.md §6.1).
#
# Per-node images are independent, so a node that fails to build is not a
# reason to abandon the ones after it: the run continues and reports every
# failure together at the end. Two nodes failing for one shared cause is then
# one diagnosis rather than two consecutive runs.
#
# What it must not do is finish quietly. This loop previously ran inline in the
# Makefile with no failure check, so the recipe took the exit status of its last
# iteration and a failure in any earlier script was discarded — `make images`
# reported success while the images for those nodes were left as they were.
#
# The directory of scripts may be given as an argument; the Makefile relies on
# the default. tests/unit/test_build_node_images.py passes one to exercise this
# against stub scripts.
#
# FMS_SRC and HMI_SRC are inherited from the environment so that each build
# script can locate the compiled binaries.
# Deliberately without `-e`, unlike the other scripts here: a failing build
# script is handled below rather than ending the run, which is the whole point.
set -uo pipefail
cd "$(dirname "$0")/.."

NODE_BUILD_DIR="${1:-build/node-build}"

if [ ! -d "$NODE_BUILD_DIR" ]; then
    echo "$NODE_BUILD_DIR not found — run 'make render' first" >&2
    exit 1
fi

# Counted rather than inferred from the glob: an unmatched glob expands to
# itself, so a directory holding no scripts would otherwise iterate once over a
# path that does not exist and report a successful build of nothing.
ran=0
failed=""

for script in "$NODE_BUILD_DIR"/*.sh; do
    [ -e "$script" ] || continue
    ran=$((ran + 1))
    echo "==> $script"
    if ! bash "$script"; then
        failed="$failed $(basename "$script" .sh)"
    fi
done

if [ "$ran" -eq 0 ]; then
    echo "no build scripts in $NODE_BUILD_DIR — run 'make render' first" >&2
    exit 1
fi

if [ -n "$failed" ]; then
    echo "ERROR: image build failed for:$failed" >&2
    echo "Those nodes' images are unchanged, so anything deployed from them is" >&2
    echo "whatever the previous build produced." >&2
    exit 1
fi

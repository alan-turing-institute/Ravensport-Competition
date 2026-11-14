#!/usr/bin/env bash
# Build the sim-host simulator container image (sim-network.md §7).
#
# Produces a minimal Alpine/OpenRC image running two processes: the static-musl
# Rust `sim` binary (loopback-only) and an HAProxy reverse proxy. The rootfs is
# bootstrapped with `apk --root` into a clean subtree — only the packages the
# two services need (§8), not the Alpine base image's full set — then customised
# (binaries, OpenRC services, a no-gettys inittab) and imported with hand-written
# metadata (templates: {}), so the base image's templates cannot regenerate /etc
# files at instance create.
#
# The sim crate lives at infra/sim/; the service definitions, networking script,
# and packet filter live in deploy/sim-host/; the HAProxy config is rendered by
# render.py to build/haproxy/sim-host.cfg.
#
# With --competition, the script builds the competition image of sim-host
# instead, ravensport/sim-competition:latest, with the competition's entry point
# (competition/sim/) in place of the simulator's own binary (episodes.md §3.4,
# competition image). The entry point compiles the simulator against the
# constants file that SIM_CONSTANTS names, when it is set (deployment-config.md
# §4.1, sim projection).
#
# Requires: cargo, musl cross-toolchain, incus, tar.

set -euo pipefail

# ── configuration ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SIM_HOST_DIR="$DEPLOY_DIR/sim-host"
HAPROXY_CFG="$DEPLOY_DIR/build/haproxy/sim-host.cfg"
CA_CERT="$DEPLOY_DIR/build/ca/ca.pem"
SIM_SRC="${SIM_SRC:-$REPO_ROOT/infra/sim}"        # the Rust sim crate
SOURCE_IMAGE="images:alpine/3.24"
ARCH="$(uname -m)"
RUST_TARGET="${ARCH}-unknown-linux-musl"
COMPETITION=0
if [ "${1:-}" = "--competition" ]; then
    COMPETITION=1
fi
if [ "$COMPETITION" = 1 ]; then
    ENTRY_SRC="$REPO_ROOT/competition/sim"         # the competition's entry point
    IMAGE_ALIAS="ravensport/sim-competition:latest"
    IMAGE_DESCRIPTION="Ravensport physics simulator, competition entry point (sim + HAProxy, Alpine/OpenRC)"
    BUILD_MANIFEST="$ENTRY_SRC/Cargo.toml"
    SIM_BIN="$ENTRY_SRC/target/${RUST_TARGET}/release/sim-competition"
    # The constants that the image compiles against.
    CONSTANTS="${SIM_CONSTANTS:-$SIM_SRC/sim-constants.toml}"
else
    ENTRY_SRC=""
    IMAGE_ALIAS="ravensport/sim:latest"
    IMAGE_DESCRIPTION="Ravensport physics simulator (sim + HAProxy, Alpine/OpenRC)"
    BUILD_MANIFEST="$SIM_SRC/Cargo.toml"
    SIM_BIN="$SIM_SRC/target/${RUST_TARGET}/release/sim"
    CONSTANTS="$SIM_SRC/sim-constants.toml"
fi

# The minimal seed (§8): base userland + OpenRC + HAProxy + networking tooling
# (iproute2-minimal for `ip`, nftables for the actor filter). apk resolves the
# rest of the closure; the locked result is tests/integration/
# sim-image.expected.packages, enforced by test-sim-image.sh.
SIM_PKGS="alpine-baselayout busybox openrc haproxy iproute2-minimal nftables"

# Throwaway build artefacts, removed on any exit (success, failure, Ctrl-C).
BUILD_DIR="$(mktemp -d)"
BUILD_CTR="sim-build-$$"
BUILD_NET="simb$$"        # short: a managed bridge's name is the kernel ifname (<=15)
cleanup() {
    incus delete -f "$BUILD_CTR" 2>/dev/null || true
    incus network delete "$BUILD_NET" 2>/dev/null || true
    rm -rf "$BUILD_DIR"
}
trap cleanup EXIT

# ── selective rebuild ─────────────────────────────────────────────────────────
# Hash all inputs before doing any work. Sources replace the binary in the hash
# so the check can run before compilation; the binary is a deterministic
# function of the source under a fixed toolchain, so this is equivalent.
test -f "$HAPROXY_CFG" || {
    echo "ERROR: rendered HAProxy config not found at $HAPROXY_CFG — run make render first." >&2
    exit 1
}
test -f "$CA_CERT" || {
    echo "ERROR: CA root certificate not found at $CA_CERT — run make build-ca first." >&2
    exit 1
}
CURRENT_HASH=$(
    {
        sha256sum "$0" "$HAPROXY_CFG" "$CA_CERT" "$CONSTANTS"
        find "$SIM_HOST_DIR" -type f -print0 | sort -z | xargs -0 sha256sum
        find "$SIM_SRC" -type f -not -path "*/target/*" -print0 | sort -z | xargs -0 sha256sum
        if [ -n "$ENTRY_SRC" ]; then
            find "$ENTRY_SRC" -type f -not -path "*/target/*" -print0 | sort -z | xargs -0 sha256sum
        fi
    } | sha256sum | cut -d' ' -f1
)
STORED_HASH=$(incus image show "$IMAGE_ALIAS" 2>/dev/null \
    | awk '/ravensport\.input-hash:/ {print $2}' || echo "")
if [ "$CURRENT_HASH" = "$STORED_HASH" ]; then
    echo "  $IMAGE_ALIAS: inputs unchanged, skipping"
    exit 0
fi

# ── build phases ─────────────────────────────────────────────────────────────

compile_sim() {
    rustup target add "$RUST_TARGET" 2>/dev/null || true
    cargo build --release --manifest-path "$BUILD_MANIFEST" --target "$RUST_TARGET"
    test -x "$SIM_BIN" || { echo "ERROR: sim binary not found at $SIM_BIN" >&2; exit 1; }
}

start_build_container() {
    # An ephemeral Incus container used only as an apk runner. apk needs egress,
    # but the deploy bridges do not exist until `make up` and the default profile
    # has no NIC — so attach a throwaway NAT bridge and wait for its DHCP lease.
    incus network create "$BUILD_NET" ipv4.address=10.99.0.1/24 ipv4.nat=true ipv6.address=none
    incus launch "$SOURCE_IMAGE" "$BUILD_CTR" --network "$BUILD_NET"
    local i
    for ((i = 0; i < 30; i++)); do
        incus exec "$BUILD_CTR" -- sh -c 'ip route | grep -q default' && return 0
        sleep 1
    done
    echo "ERROR: $BUILD_CTR has no default route after 30s on $BUILD_NET;" \
         "it cannot reach the package mirror" >&2
    exit 1
}

bootstrap_rootfs() {
    # Install the minimal seed into a clean /rootfs, signature-verified via the
    # build container's keys — so the base image's extra packages, apk-tools, and
    # signing keys never reach the image.
    # shellcheck disable=SC2086  # SIM_PKGS is an intentional word list
    incus exec "$BUILD_CTR" -- apk --root /rootfs --initdb \
        --keys-dir /etc/apk/keys --repositories-file /etc/apk/repositories --no-cache \
        add $SIM_PKGS
}

customise_rootfs() {
    # Push the sim binary and the sim-host/ artefacts in, then customise via chroot
    # (service users, file installs, the no-gettys inittab, the OpenRC runlevel).
    incus file push --create-dirs --mode=0755 "$SIM_BIN" "$BUILD_CTR/rootfs/in/sim"
    incus file push -r "$SIM_HOST_DIR" "$BUILD_CTR/rootfs/"
    incus file push --mode=0644 "$HAPROXY_CFG" "$BUILD_CTR/rootfs/sim-host/haproxy.cfg"
    # Root CA certificate, so HAProxy can validate hmi-01 on the dock-operator
    # backend instead of `verify none` (digital.md §8 decision 7). Trust-only:
    # sim-host is a TLS client here, never presents a certificate of its own.
    incus file push --create-dirs --mode=0644 "$CA_CERT" "$BUILD_CTR/rootfs/etc/haproxy/tls/ca.pem"
    # The body runs in the rootfs's shell, so $svc et al. must reach it unexpanded.
    # shellcheck disable=SC2016
    incus exec "$BUILD_CTR" -- chroot /rootfs /bin/sh -euc '
    # Unprivileged service users — one each, no login, no home.
    addgroup -S sim 2>/dev/null || true
    adduser -S -D -H -s /sbin/nologin -G sim sim 2>/dev/null || true
    addgroup -S haproxy 2>/dev/null || true
    adduser -S -D -H -s /sbin/nologin -G haproxy haproxy 2>/dev/null || true

    # /etc/haproxy/tls/ was created by the host-side file push above, before the
    # haproxy user existed, so it is owned by the incus build identity and not
    # traversable by anyone else. Fix ownership now that haproxy exists, so the
    # unprivileged haproxy process (command_user in init.d/haproxy) can read
    # ca.pem instead of failing to start.
    chown -R root:haproxy /etc/haproxy/tls
    chmod 750 /etc/haproxy/tls
    chmod 644 /etc/haproxy/tls/ca.pem

    # Binaries and configuration.
    install -D -m 0755 /in/sim                    /usr/local/bin/sim
    install -D -m 0755 /sim-host/sim-net.sh       /usr/local/bin/sim-net.sh
    install -D -m 0644 /sim-host/haproxy.cfg      /etc/haproxy/haproxy.cfg
    install -D -m 0644 /sim-host/egress-guard.nft /etc/nftables/egress-guard.nft
    install -D -m 0755 /sim-host/init.d/sim-net   /etc/init.d/sim-net
    install -D -m 0755 /sim-host/init.d/sim       /etc/init.d/sim
    install -D -m 0755 /sim-host/init.d/haproxy   /etc/init.d/haproxy

    # Container-appropriate init: run the OpenRC runlevels, no gettys. The image
    # is imported with templates: {}, so this file is authoritative.
    cat > /etc/inittab <<INITTAB
::sysinit:/sbin/openrc sysinit
::wait:/sbin/openrc boot
::wait:/sbin/openrc default
::shutdown:/sbin/openrc shutdown
INITTAB

    # Tell OpenRC it is inside an (LXC-based) container, and enable only our three
    # services in the default runlevel.
    echo "rc_sys=\"lxc\"" >> /etc/rc.conf
    mkdir -p /etc/runlevels/sysinit /etc/runlevels/boot /etc/runlevels/default /etc/runlevels/shutdown
    for svc in sim-net haproxy sim; do
        ln -sf /etc/init.d/$svc /etc/runlevels/default/$svc
    done

    # Writable runtime paths (the rest of the rootfs is immutable, §8) and empty
    # mountpoints for the virtual filesystems LXC mounts at start. Remove all apk
    # state (the build mirror URL, keys, world/arch, cache): inert with no
    # apk-tools in the image, so dropped rather than baked in. Drop the build inputs.
    mkdir -p /run /var/log /dev /proc /sys
    # The mount point for the read-only state directory the deployment supplies
    # on a warm start (sim-network.md §7); empty, so the sim cold-starts
    # without it.
    mkdir -p /var/lib/sim/state
    rm -rf /var/cache/apk/* /etc/apk /in /sim-host
'

    # The competition image's empty mount point of the start-up input, which the
    # deployment of a competition episode attaches read-only (episodes.md §4.3,
    # start-up input). It shares no path with the checkpoint's mount directory
    # above, which only a restore attaches (deployment.md §10).
    if [ "$COMPETITION" = 1 ]; then
        incus exec "$BUILD_CTR" -- mkdir -p /rootfs/var/lib/sim/startup
    fi
}

import_image() {
    # /rootfs is static (nothing runs from it), so the tar is clean; keep the
    # virtual-filesystem mountpoints but not their contents. Import with explicit
    # metadata (templates: {}) — see the header note.
    incus exec "$BUILD_CTR" -- tar \
        --exclude='./dev/*' --exclude='./proc/*' --exclude='./sys/*' \
        -C /rootfs -czf /tmp/rootfs.tar.gz .
    incus file pull "$BUILD_CTR/tmp/rootfs.tar.gz" "$BUILD_DIR/rootfs.tar.gz"

    cat > "$BUILD_DIR/metadata.yaml" <<EOF
architecture: ${ARCH}
creation_date: $(date +%s)
properties:
  description: ${IMAGE_DESCRIPTION}
  os: alpine
  release: sim-host
  ravensport.input-hash: ${CURRENT_HASH}
config:
  security.privileged: "false"
templates: {}
EOF
    tar -C "$BUILD_DIR" -czf "$BUILD_DIR/metadata.tar.gz" metadata.yaml

    # Replacing the alias deletes the old image (no accumulation). The delete->import
    # window is not atomic; if interrupted there, re-run `make images`.
    incus image delete "$IMAGE_ALIAS" 2>/dev/null || true
    incus image import "$BUILD_DIR/metadata.tar.gz" "$BUILD_DIR/rootfs.tar.gz" --alias "$IMAGE_ALIAS"
}

# ── run ──────────────────────────────────────────────────────────────────────
echo "==> Building $IMAGE_ALIAS from $BUILD_MANIFEST (target: ${RUST_TARGET})"
compile_sim
start_build_container
bootstrap_rootfs
customise_rootfs
import_image
echo "==> Image imported as $IMAGE_ALIAS"

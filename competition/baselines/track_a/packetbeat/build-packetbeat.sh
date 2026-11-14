#!/usr/bin/env bash
# Builds Packetbeat's open-licence edition from its pinned source release, for
# the two architectures of the Incus hosts: amd64, and arm64 for the Linux
# guests of a macOS machine with an Apple processor (README.md, "Building
# Packetbeat").
#
# The edition is the Apache License 2.0 build: the packetbeat directory of the
# Beats repository, whose dependencies include no code under the Elastic
# License. Elastic's own builds of it carry "-oss" in their names. The build
# checks the licence of every package that it links.
#
# Packetbeat needs cgo, the Go toolchain's interface to C, whichever capture
# method it uses: the Go packet library imports C for both the AF_PACKET
# socket and libpcap, and the AF_PACKET reader compiles its Berkeley Packet
# Filter (BPF) expression with libpcap. libpcap is therefore built from its own
# pinned release, without its optional capture of D-Bus, remote direct memory
# access, Universal Serial Bus, Bluetooth and netmap, and linked statically.
# The C library is linked dynamically. The build runs in a container of
# Ubuntu 24.04, the release of siem-01's image, so the binary needs no C
# library newer than siem-01's.
#
# Usage:
#   build-packetbeat.sh [OUTPUT_DIR]
#       Runs the build in a container of the pinned image with Docker, and
#       writes the two binaries, their SHA-256 digests (SHA256SUMS) and a
#       record of the build (build-record.json) to OUTPUT_DIR, by default
#       dist/ beside this script. Downloads and the Go module cache are kept in
#       OUTPUT_DIR/cache for a later build. BUILD_CPUS and BUILD_MEMORY limit
#       the container, by default to 8 processors and 8 GiB.
#   build-packetbeat.sh --in-container OUTPUT_DIR
#       The build itself, in any Ubuntu 24.04 system with network access, as
#       root.
set -euo pipefail

# ── Pinned inputs ──────────────────────────────────────────────────────────────
# The release: Beats v9.5.5, published on 6 October 2026, a stable release
# more than two weeks old when it was pinned.
BEATS_TAG="v9.5.5"
BEATS_VERSION="${BEATS_TAG#v}"
BEATS_COMMIT="b80bf8464fba72f582cefed91dd72909eb7116db"
BEATS_ARCHIVE_URL="https://github.com/elastic/beats/archive/refs/tags/${BEATS_TAG}.tar.gz"
BEATS_ARCHIVE_SHA256="ae4dae382249f28a53be500c0c5d5a6628677f3e65a7d35dfc148b9ad4dc0635"
# The Go release that the source's go.mod and .go-version name.
GO_VERSION="1.26.8"
GO_SHA256_AMD64="d0f743b33e8d8945e6b1f432edd15785c70507121d6e2a723b21285eddf8b57b"
GO_SHA256_ARM64="211ffced9dcb9633a55eac6364816ec0ddd951389a740e88fa8b3337971bdda0"
# libpcap 1.10.7, of 5 September 2026, the newest release of the 1.10 line.
# The digest is of the archive whose signature verified against The Tcpdump
# Group's package signing key, 1F166A5742ABB9E0249A8D30E089DEF1D9C15D0D.
LIBPCAP_VERSION="1.10.7"
LIBPCAP_URL="https://www.tcpdump.org/release/libpcap-${LIBPCAP_VERSION}.tar.xz"
LIBPCAP_SHA256="68fa62cffb974f4275641ce14c2e2d75739251f30e00e6a0900903b247d76a03"
# Ubuntu 24.04, by the digest of its multi-architecture index.
BASE_IMAGE="docker.io/library/ubuntu:24.04@sha256:2198e4c65076e9cb0b3d695219df667c0e3b0072da64ca3c2648aca17dcecaf5"

TARGETS=(amd64 arm64)
# The licence types of NOTICE.txt that the binary may link. The source's
# NOTICE.txt gives the licence type of each third-party module.
OPEN_LICENCES='^(Apache-2\.0|MIT|BSD|BSD-2-Clause|BSD-2-Clause-FreeBSD|BSD-3-Clause|ISC|MPL-2\.0|CC0-1\.0)$'

binary_name() { echo "packetbeat-oss-${BEATS_VERSION}-linux-$1"; }

# ── In the container ───────────────────────────────────────────────────────────

fetch() { # URL SHA256 FILE
    local url=$1 sha256=$2 file=$3
    if [ ! -f "$file" ] || ! echo "$sha256  $file" | sha256sum -c --status -; then
        rm -f "$file"
        curl -fsSL --retry 3 -o "$file.part" "$url"
        mv "$file.part" "$file"
    fi
    echo "$sha256  $file" | sha256sum -c -
}

triplet() { # ARCH
    case "$1" in
        amd64) echo x86_64-linux-gnu ;;
        arm64) echo aarch64-linux-gnu ;;
    esac
}

c_compiler() { # ARCH
    if [ "$1" = "$NATIVE" ]; then echo gcc; else echo "$(triplet "$1")-gcc"; fi
}

build_libpcap() { # ARCH
    local arch=$1 src="$WORK/libpcap-src-$1"
    rm -rf "$src" "$WORK/libpcap-$arch"
    mkdir -p "$src"
    tar -xJf "$CACHE/libpcap-${LIBPCAP_VERSION}.tar.xz" -C "$src" --strip-components=1
    (
        cd "$src"
        CC="$(c_compiler "$arch")" CFLAGS="-O2 -fPIC" ./configure --quiet \
            --build="$(triplet "$NATIVE")" --host="$(triplet "$arch")" \
            --prefix="$WORK/libpcap-$arch" \
            --disable-shared --with-pcap=linux \
            --disable-dbus --disable-rdma --disable-usb --disable-bluetooth \
            --disable-netmap --disable-remote --without-libnl --without-dpdk \
            --without-dag --without-septel --without-snf --without-turbocap
        make -s -j"$(nproc)"
        make -s install
    )
}

with_go_env() { # ARCH COMMAND...
    local arch=$1
    shift
    GOOS=linux GOARCH="$arch" CGO_ENABLED=1 CC="$(c_compiler "$arch")" \
        CGO_CFLAGS="-O2 -g -I$WORK/libpcap-$arch/include" \
        CGO_LDFLAGS="-L$WORK/libpcap-$arch/lib" "$@"
}

check_licences() { # ARCH
    local arch=$1 module licence failed=0
    cd "$WORK/beats"
    # No package of the x-pack directory, which holds the Elastic-licensed code.
    if with_go_env "$arch" go list -deps ./packetbeat \
        | grep -E '^github\.com/elastic/beats/v7/x-pack(/|$)'; then
        echo "ERROR: the build for $arch imports the packages above from x-pack" >&2
        exit 1
    fi
    # Every third-party module that the build links carries an open licence.
    with_go_env "$arch" go list -deps \
        -f '{{with .Module}}{{if .Replace}}{{.Replace.Path}}{{else}}{{.Path}}{{end}}{{end}}' \
        ./packetbeat | sort -u > "$WORK/modules-$arch.txt"
    : > "$WORK/licences-$arch.txt"
    while read -r module; do
        [ -n "$module" ] || continue
        [ "$module" = "github.com/elastic/beats/v7" ] && continue
        licence=$(awk -v m="$module" '
            /^Dependency : / { dep = $3 }
            /^Licence type \(autodetected\): / && dep == m {
                sub(/^Licence type \(autodetected\): /, ""); print; exit }
        ' NOTICE.txt)
        echo "$module ${licence:-unlisted}" >> "$WORK/licences-$arch.txt"
        if ! echo "$licence" | grep -Eq "$OPEN_LICENCES"; then
            echo "ERROR: $module carries the licence '${licence:-unlisted}'" >&2
            failed=1
        fi
    done < "$WORK/modules-$arch.txt"
    [ "$failed" = 0 ] || exit 1
}

build_packetbeat() { # ARCH
    local arch=$1
    cd "$WORK/beats"
    with_go_env "$arch" go build -trimpath -buildvcs=false \
        -ldflags "-s -w -X github.com/elastic/beats/v7/libbeat/version.vcsRevision=$BEATS_COMMIT" \
        -o "$OUT/$(binary_name "$arch")" ./packetbeat
}

check_binary() { # ARCH
    local arch=$1 bin
    bin="$OUT/$(binary_name "$arch")"
    # The dynamic section names the C library and no libpcap.
    readelf -d "$bin" | awk '/NEEDED/ { print $NF }' | tr -d '[]' > "$WORK/needed-$arch.txt"
    if grep -q pcap "$WORK/needed-$arch.txt"; then
        echo "ERROR: $bin links libpcap dynamically" >&2
        exit 1
    fi
    go version -m "$bin" > "$WORK/go-version-$arch.txt"
}

# A capture through the entry's own configuration, on the loopback interface
# and its address in place of siem-01's: one connection attempt to the
# enrolment service's port, one TLS handshake with the management interface's
# port, and one opening of the agent channel. The binary runs with the
# capabilities that its systemd unit leaves it.
smoke_test() {
    local bin dir="$WORK/smoke"
    bin="$OUT/$(binary_name "$NATIVE")"
    rm -rf "$dir"
    mkdir -p "$dir/data" "$dir/logs"
    sed -e 's/device: eth0/device: lo/' -e 's/10\.30\.0\.30/127.0.0.1/g' \
        "$ENTRY/packetbeat.yml" > "$dir/packetbeat.yml"
    chmod 0600 "$dir/packetbeat.yml"
    "$bin" version
    "$bin" test config -c "$dir/packetbeat.yml" \
        --path.data "$dir/data" --path.logs "$dir/logs"
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=siem-01 \
        -keyout "$dir/key.pem" -out "$dir/cert.pem" 2> /dev/null
    openssl s_server -quiet -accept 55000 -cert "$dir/cert.pem" -key "$dir/key.pem" \
        > /dev/null 2>&1 &
    local server=$!
    setpriv --inh-caps=-all --bounding-set=-all,+net_raw --no-new-privs \
        "$bin" -c "$dir/packetbeat.yml" --path.data "$dir/data" --path.logs "$dir/logs" \
        > "$OUT/smoke-events.ndjson" &
    local capture=$!
    sleep 5
    (echo > /dev/tcp/127.0.0.1/1515) 2> /dev/null || true
    (echo > /dev/tcp/127.0.0.1/1514) 2> /dev/null || true
    echo | timeout 10 openssl s_client -connect 127.0.0.1:55000 -servername siem-01 \
        > /dev/null 2>&1 || true
    sleep 16
    kill "$capture" "$server"
    wait "$capture" || true
    wait "$server" 2> /dev/null || true
    for port in 1514 1515 55000; do
        if ! grep -q "\"port\":${port}[,}]" "$OUT/smoke-events.ndjson"; then
            echo "ERROR: the capture holds no event of port $port" >&2
            tail -n 20 "$dir/logs"/* >&2 || true
            exit 1
        fi
    done
    grep -q '"type":"tls"' "$OUT/smoke-events.ndjson" \
        || { echo "ERROR: the capture holds no TLS event" >&2; exit 1; }
    echo "smoke test: $(wc -l < "$OUT/smoke-events.ndjson") events"
}

record() {
    {
        echo "{"
        echo "  \"beats_tag\": \"$BEATS_TAG\","
        echo "  \"beats_commit\": \"$BEATS_COMMIT\","
        echo "  \"beats_archive_url\": \"$BEATS_ARCHIVE_URL\","
        echo "  \"beats_archive_sha256\": \"$BEATS_ARCHIVE_SHA256\","
        echo "  \"go_version\": \"$GO_VERSION\","
        echo "  \"libpcap_version\": \"$LIBPCAP_VERSION\","
        echo "  \"libpcap_sha256\": \"$LIBPCAP_SHA256\","
        echo "  \"base_image\": \"$BASE_IMAGE\","
        echo "  \"build_architecture\": \"$NATIVE\","
        echo "  \"packages\": ["
        dpkg-query -W -f='    "${Package}=${Version}",\n' "${PACKAGES[@]}" | sed '$ s/,$//'
        echo "  ],"
        echo "  \"binaries\": {"
        local first=1 arch
        for arch in "${TARGETS[@]}"; do
            [ "$first" = 1 ] || echo ","
            first=0
            printf '    "%s": {"sha256": "%s", "needed": [%s]}' "$(binary_name "$arch")" \
                "$(sha256sum "$OUT/$(binary_name "$arch")" | cut -d' ' -f1)" \
                "$(sed 's/.*/"&"/' "$WORK/needed-$arch.txt" | paste -sd, -)"
        done
        echo
        echo "  }"
        echo "}"
    } > "$OUT/build-record.json"
    for arch in "${TARGETS[@]}"; do
        cp "$WORK/licences-$arch.txt" "$OUT/licences-$arch.txt"
        cp "$WORK/go-version-$arch.txt" "$OUT/go-version-$arch.txt"
    done
    local names=()
    for arch in "${TARGETS[@]}"; do
        names+=("$(binary_name "$arch")")
    done
    (cd "$OUT" && sha256sum "${names[@]}" > SHA256SUMS)
}

in_container() {
    OUT=$1
    ENTRY=$(cd "$(dirname "$0")" && pwd)
    CACHE="$OUT/cache"
    WORK=/build
    NATIVE=$(dpkg --print-architecture)
    mkdir -p "$CACHE" "$WORK"
    if [ -n "${OUT_OWNER:-}" ]; then
        trap 'chown -R "$OUT_OWNER" "$OUT"' EXIT
    fi

    PACKAGES=(ca-certificates curl xz-utils build-essential flex bison binutils openssl util-linux)
    case "$NATIVE" in
        amd64) PACKAGES+=(gcc-aarch64-linux-gnu libc6-dev-arm64-cross) ;;
        arm64) PACKAGES+=(gcc-x86-64-linux-gnu libc6-dev-amd64-cross) ;;
        *) echo "ERROR: unsupported build architecture $NATIVE" >&2; exit 1 ;;
    esac
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends "${PACKAGES[@]}" > /dev/null

    fetch "$BEATS_ARCHIVE_URL" "$BEATS_ARCHIVE_SHA256" "$CACHE/beats-${BEATS_TAG}.tar.gz"
    fetch "$LIBPCAP_URL" "$LIBPCAP_SHA256" "$CACHE/libpcap-${LIBPCAP_VERSION}.tar.xz"
    local go_sha256
    case "$NATIVE" in
        amd64) go_sha256=$GO_SHA256_AMD64 ;;
        arm64) go_sha256=$GO_SHA256_ARM64 ;;
    esac
    fetch "https://go.dev/dl/go${GO_VERSION}.linux-${NATIVE}.tar.gz" "$go_sha256" \
        "$CACHE/go${GO_VERSION}.linux-${NATIVE}.tar.gz"

    rm -rf "$WORK/go" "$WORK/beats"
    tar -xzf "$CACHE/go${GO_VERSION}.linux-${NATIVE}.tar.gz" -C "$WORK"
    mkdir -p "$WORK/beats"
    tar -xzf "$CACHE/beats-${BEATS_TAG}.tar.gz" -C "$WORK/beats" --strip-components=1
    export PATH="$WORK/go/bin:$PATH"
    # The pinned toolchain alone, and the modules exactly as the source's go.sum
    # names them, checked against the Go checksum database.
    export GOTOOLCHAIN=local GOFLAGS=-mod=readonly GOPROXY=https://proxy.golang.org
    export GOSUMDB=sum.golang.org GOMODCACHE="$CACHE/gomod" GOCACHE="$CACHE/gocache"

    local arch
    for arch in "${TARGETS[@]}"; do
        build_libpcap "$arch"
        check_licences "$arch"
        build_packetbeat "$arch"
        check_binary "$arch"
    done
    smoke_test
    record
    cat "$OUT/SHA256SUMS"
}

# ── On the host ────────────────────────────────────────────────────────────────

on_host() {
    local here out
    here=$(cd "$(dirname "$0")" && pwd)
    out=${1:-$here/dist}
    mkdir -p "$out"
    out=$(cd "$out" && pwd)
    docker run --rm --name "packetbeat-build-$$" \
        --cpus "${BUILD_CPUS:-8}" --memory "${BUILD_MEMORY:-8g}" \
        --env OUT_OWNER="$(id -u):$(id -g)" \
        --mount "type=bind,src=$here,dst=/entry,readonly" \
        --mount "type=bind,src=$out,dst=/out" \
        "$BASE_IMAGE" \
        nice -n 10 bash /entry/build-packetbeat.sh --in-container /out
}

if [ "${1:-}" = "--in-container" ]; then
    shift
    in_container "${1:?usage: build-packetbeat.sh --in-container OUTPUT_DIR}"
else
    on_host "$@"
fi

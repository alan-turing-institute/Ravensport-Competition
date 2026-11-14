PROFILE ?= ci-minimal
WORLD    ?= world.yaml
# An additional override file, applied after the profile's (deployment.md §7.3).
# The competition's is ../competition/deploy/override.yaml.
EXTRA_OVERRIDE ?=
FMS_SRC  ?= ../fms
HMI_SRC  ?= ../hmi

# Attack-phase tooling. The adversary launch host is deployed from its own
# `adversary` build script and carries no service; its image holds every attack
# binary and chains.json from ATTACK_SRC/dist, which `make up` populates before
# the image is built (adversary.md §2). With the generator source present the
# emitter (`make build-attacks`) populates dist; the public release ships without
# the source, so `make up` then copies the prebuilt binaries for this
# architecture (PREBUILT/$(ARCH)) into dist instead. ADVERSARY is the Incus
# instance name.
ATTACK_SRC   ?= ../attack-chains
ADVERSARY    ?= adversary
ATTACK_DEST  ?= /root
DIST         := $(ATTACK_SRC)/dist
PREBUILT     := $(ATTACK_SRC)/prebuilt
# The target architecture, as the Incus host reports it: x86_64 or aarch64. The
# prebuilt binaries are grouped by it (PREBUILT/<arch>/).
ARCH         := $(shell uname -m)

# Python tooling (render, unit tests, ruff) runs from a project-local
# virtualenv so `make install` and every target that depends on it behave
# identically on the Lima VM and a bare host, with no system-Python (PEP 668)
# override. PYTHON is only the bootstrap interpreter used to create the venv.
#
# The venv directory name encodes the host OS and architecture. Lima mounts the
# whole home directory into the VM, so the repository — and any venv inside it —
# is shared between the macOS host and the Linux VM. A single `.venv` built on
# one would present an interpreter the other cannot run; the platform suffix
# gives each its own directory so they coexist on the shared mount.
PYTHON ?= python3
VENV   := .venv-$(shell uname -s)-$(shell uname -m)
STAMP  := $(VENV)/.stamp
PY     := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip
RUFF   := $(VENV)/bin/ruff
PYTEST := $(VENV)/bin/pytest

.PHONY: help install render build-fms build-hmi build-binaries build-ca \
        build-sim-image build-sim-competition-image build-node-images images build deploy up down \
        build-attacks build-attacks-mock stage-attacks write-prebuilt push-attacks \
        snapshot export-snapshot restore stop \
        guard-nft require-incus require-clock-released \
        test test-unit test-integration \
        test-image-build test-boot test-smoke test-firewall test-sim-image \
        test-snapshot \
        lint clean ssh logs

help:
	@echo "Targets:"
	@echo "  make install            Build the .venv with renderer/lint deps"
	@echo "  make render             Render build/ for PROFILE=$(PROFILE) WORLD=$(WORLD)"
	@echo "  make build-fms          Compile the FMS binary (release)"
	@echo "  make build-hmi          Compile the HMI binary (release)"
	@echo "  make build-binaries     Compile FMS and HMI binaries"
	@echo "  make build-ca           Generate the internal CA and sign its leaf certificates"
	@echo "  make build-sim-image    Build the sim-host Incus image"
	@echo "  make build-sim-competition-image  Build sim-host's competition image"
	@echo "  make build-node-images  Build per-node Incus images from build/node-build/"
	@echo "  make images             Build all Incus images (sim and per-node)"
	@echo "  make build              Render and build all images"
	@echo "  make deploy             Start instances and wait for readiness (requires make build)"
	@echo "  make up                 Build, deploy, then build and push the real attack binaries"
	@echo "  make down [FORCE=1]     Tear everything down; FORCE=1 also deletes snapshot sets"
	@echo "                          and stops a restored run"
	@echo ""
	@echo "  make snapshot LABEL=<label>         Stop the environment and snapshot it"
	@echo "  make export-snapshot LABEL=<label>  Write a snapshot set to one file"
	@echo "  make restore SNAPSHOT=<label|file> [SEED=<n>]"
	@echo "                                      Start a restored run from a snapshot set or file"
	@echo "  make stop                           Stop a restored run and release the host clock"
	@echo ""
	@echo "  make test-unit          Unit tests only — no deployment needed"
	@echo "  make test-image-build   Selective rebuild and config-propagation checks (needs make images)"
	@echo "  make test-boot          Post-deployment boot checks: fw config, bridge isolation, services (needs deployment)"
	@echo "  make test-smoke         Core data-path smoke test (needs deployment)"
	@echo "  make test-firewall      Firewall policy tests (needs deployment)"
	@echo "  make test-sim-image     sim-host image minimalism / spec checks (needs make images)"
	@echo "  make test-snapshot      Snapshot sets: stops the environment and steps the host clock (needs deployment)"
	@echo "  make test-integration   All integration tests in order (needs deployment)"
	@echo "  make test               Unit + integration (needs deployment)"
	@echo ""
	@echo "  make lint               yamllint + shellcheck + ruff"
	@echo "  make ssh NODE=fms-01    Shell into a node"
	@echo "  make logs NODE=fms-01   journalctl on a node"
	@echo "  make clean              Remove build/"
	@echo ""
	@echo "  make build-attacks      Emit the real attack task binaries into $(ATTACK_SRC)/dist (needs the source)"
	@echo "  make build-attacks-mock Emit the test-only mock set (prefixed mock-scan-test__) for make test-attack"
	@echo "  make write-prebuilt     Write $(PREBUILT)/$(ARCH)/ from a fresh build, for a source-free release"
	@echo "  make push-attacks       Push attack binaries and chains.json onto the $(ADVERSARY) host"
	@echo ""
	@echo "Profiles: ci-minimal (default), full"
	@echo "  EXTRA_OVERRIDE=<path> applies an additional override file after the profile's"
	@echo "  full requires KVM; not available on Apple Silicon < M3."

install: $(STAMP)

# Rebuilt whenever requirements.txt changes. The venv keeps the renderer, test,
# and lint Python dependencies off the system interpreter, so the same
# `make install` works on the Lima VM and a bare host without a PEP 668 override.
$(STAMP): requirements.txt
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	touch $(STAMP)

render: $(STAMP)
	$(PY) scripts/render.py --profile $(PROFILE) --world $(WORLD) \
		$(if $(EXTRA_OVERRIDE),--extra-override $(EXTRA_OVERRIDE))

# Fail early with a clear message if Incus is unavailable, rather than dying on
# a raw `incus` error partway through a deploy or integration run.
require-incus:
	bash scripts/require-incus.sh

build-fms:
	cd $(FMS_SRC) && SQLX_OFFLINE=true cargo build --release
	@echo "Binary at: $(FMS_SRC)/target/release/fms"

build-hmi:
	cd $(HMI_SRC) && cargo build --release
	@echo "Binary at: $(HMI_SRC)/target/release/hmi"

build-binaries: build-fms build-hmi

# Generates the offline internal CA and signs the fms-01/hmi-01 leaf
# certificates (digital.md §2.7, §8 decision 7). Idempotent: a re-run is a
# no-op unless world.yaml's crypto.password_salt_seed changed (regenerating
# byte-identical output for an unchanged seed) or build/ was removed.
# Requires render to have produced build/ca's key material first.
build-ca: render
	bash scripts/build-ca.sh

build-sim-image: build-ca
	bash scripts/build-sim-image.sh

# The competition image of sim-host, with the competition's entry point in place
# of the simulator's binary (episodes.md §3.4, competition image). A render at
# a compression factor other than 1 writes the scaled constants to
# build/sim/sim-constants.toml, and SIM_CONSTANTS names that file, against
# which the image compiles the simulator (deployment-config.md §4.1, sim
# projection). A render at the factor 1 writes no scaled file, and the image
# compiles against the committed constants, which hold the same values.
SCALED_SIM_CONSTANTS := build/sim/sim-constants.toml
build-sim-competition-image: build-ca
	if [ -f $(SCALED_SIM_CONSTANTS) ]; then \
		export SIM_CONSTANTS="$(CURDIR)/$(SCALED_SIM_CONSTANTS)"; \
	fi; \
	bash scripts/build-sim-image.sh --competition

# Compiles FMS and HMI binaries, then runs each per-node build script from
# build/node-build/. Requires render to have produced build/node-build/ first.
# FMS_SRC and HMI_SRC are forwarded so the build scripts can locate the binaries.
# The loop lives in the script so that its failure handling is covered by
# tests/unit/test_build_node_images.py rather than asserted by reading this file.
build-node-images: build-binaries build-ca
	FMS_SRC=$(FMS_SRC) HMI_SRC=$(HMI_SRC) bash scripts/build-node-images.sh

# Build all Incus images: the sim-host image and every per-node image, and, with
# an additional override file, sim-host's competition image (episodes.md §3.4,
# competition image). Per-node images are built after render has produced
# build/node-build/.
images: build-sim-image $(if $(EXTRA_OVERRIDE),build-sim-competition-image) build-node-images

# Full build: render the topology then build all images.
build: render images

deploy: require-incus
	bash scripts/deploy.sh

# Checked before the build as well as by deploy.sh, so that a restored run in
# progress is reported before the images are built rather than after.
up: require-incus require-clock-released stage-attacks build deploy

# Refuses, changing nothing, while a restored run is in progress or any instance holds
# a snapshot set, unless FORCE=1 (deployment.md §8).
down:
	bash scripts/teardown.sh "$(FORCE)"

# Snapshot sets (deployment.md §8.1, §8.2, §10).
snapshot: require-incus
	@test -n "$(LABEL)" || { echo "Usage: make snapshot LABEL=<label>"; exit 1; }
	bash scripts/snapshot.sh "$(LABEL)"

export-snapshot: require-incus
	@test -n "$(LABEL)" || { echo "Usage: make export-snapshot LABEL=<label>"; exit 1; }
	bash scripts/export-snapshot.sh "$(LABEL)"

# A restore reads only the snapshot set or file: not PROFILE, WORLD or build/.
restore: require-incus
	@test -n "$(SNAPSHOT)" || { echo "Usage: make restore SNAPSHOT=<label|file> [SEED=<n>]"; exit 1; }
	bash scripts/restore.sh "$(SNAPSHOT)" "$(SEED)"

stop: require-incus
	bash scripts/stop.sh

# Refuses while a restored run holds the host clock (deployment.md §8.2).
require-clock-released:
	@bash -c 'source scripts/snapshot-lib.sh && require_clock_released'

guard-nft:
	nft -f nftables/sim-guard.nft

test-unit: $(STAMP)
	$(PYTEST) tests/unit -q

test-image-build:
	bash tests/integration/test_image_build.sh

test-boot:
	bash tests/integration/test_boot.sh

test-smoke:
	bash tests/integration/test_smoke.sh

test-firewall:
	bash tests/integration/test_firewall.sh
	PROFILE=$(PROFILE) bash tests/integration/test_policy.sh

test-sim-image:
	bash tests/integration/test_sim_image.sh

# Not part of test-integration: it stops the environment, steps the host clock
# and can take up to an hour while the sim comes to rest.
test-snapshot: require-incus
	bash tests/integration/test_snapshot.sh

test-integration: require-incus test-image-build test-boot test-smoke test-firewall test-sim-image

test: test-unit test-integration

lint: $(STAMP)
	$(RUFF) check scripts/ tests/
	yamllint -d "{extends: default, rules: {line-length: disable, braces: {min-spaces-inside: 0, max-spaces-inside: -1}, commas: {max-spaces-after: -1}, document-start: disable}}" topology.yaml overrides/
	shellcheck scripts/*.sh tests/integration/*.sh

ssh:
	@test -n "$(NODE)" || { echo "Usage: make ssh NODE=<name>"; exit 1; }
	incus exec $(NODE) -- bash

logs:
	@test -n "$(NODE)" || { echo "Usage: make logs NODE=<name>"; exit 1; }
	incus exec $(NODE) -- journalctl -xe --no-pager | tail -n 100

# The attack step of `make up`: populate ATTACK_SRC/dist so the adversary image
# build (which reads dist) is identical whether or not the generator source
# ships. With the source present it builds the binaries. In the public release,
# which ships without the source, it copies the prebuilt binaries for this
# architecture into dist and verifies them against SHA256SUMS, carrying
# chains.json, SHA256SUMS and SOURCE_SHA256 (the review digest the episode runner
# checks when the source is absent) along. It fails clearly when neither the
# source nor a prebuilt set for this architecture is present.
stage-attacks:
	@set -e; \
	if [ -f $(ATTACK_SRC)/Cargo.toml ]; then \
		$(MAKE) build-attacks; \
	else \
		src="$(PREBUILT)/$(ARCH)"; \
		test -d "$$src" || { \
			echo "no attack-chains source and no prebuilt binaries for $(ARCH) at $$src" >&2; \
			echo "(this release ships prebuilt/<arch>/; $(ARCH) is not among them)" >&2; \
			exit 1; }; \
		test -f "$$src/SHA256SUMS" || { echo "$$src has no SHA256SUMS" >&2; exit 1; }; \
		echo "==> stage prebuilt attack binaries for $(ARCH) into $(DIST)"; \
		rm -rf "$(DIST)"; mkdir -p "$(DIST)"; \
		cp "$$src"/* "$(DIST)"/; \
		( cd "$(DIST)" && sha256sum -c SHA256SUMS ) || { \
			echo "prebuilt checksum verification failed in $(DIST)" >&2; exit 1; }; \
		echo "Staged prebuilt attack artifacts in: $(DIST)"; \
	fi

# Emit the attack task binaries into ATTACK_SRC/dist. Runs the attack-chains
# emitter, which compiles, strips, and verifies each task with the hardened
# release profile; the emitter binary need
# not be release, so this runs it in debug for a faster turnaround.
#
# Cargo features forwarded to the emitter (and, through it, to the task crate).
# Empty for a normal build: the real-scan binaries under their bare names, which
# is what `make up` builds into the adversary's image. `build-attacks-mock` sets it to
# the test-only mock feature.
ATTACK_FEATURES ?=

# Guarded to Linux: cargo builds natively, so on a macOS host this would emit
# Mach-O binaries that cannot run on the adversary container (and would silently
# overwrite good Linux ELF ones). The crate still *compiles* on macOS for the
# pre-commit clippy checks — that is `cargo clippy`, not this target. Run this
# inside the Lima VM.
build-attacks:
	@test -f $(ATTACK_SRC)/Cargo.toml || { \
		echo "the attack-chains generator source is not part of this release" >&2; \
		echo "($(ATTACK_SRC)/Cargo.toml is absent). build-attacks, build-attacks-mock and" >&2; \
		echo "test-attack need it; 'make up' stages the prebuilt binaries instead." >&2; \
		exit 1; }
	@test "$$(uname -s)" = Linux || { \
		echo "build-attacks must run on Linux (the Lima VM): cargo builds natively," >&2; \
		echo "so on $$(uname -s) it emits binaries the Linux adversary cannot run." >&2; \
		echo "Enter the VM (e.g. 'limactl shell <vm>') and run 'make build-attacks' there." >&2; \
		exit 1; }
	cd $(ATTACK_SRC) && cargo run --locked --bin attack-chain $(if $(ATTACK_FEATURES),--features $(ATTACK_FEATURES),)
	@echo "Attack artifacts in: $(ATTACK_SRC)/dist"

# Emit the TEST-ONLY mock set for the attack-chains integration suite: the task
# binaries' `scan-it-segment` stage returns hardcoded facts instead of running
# the live scan, every binary name is prefixed `mock-scan-test__`, and an extra
# `mock-scan-test__scan-contract` binary runs the REAL scan to verify those facts
# (attack-chains/src/library.rs, attack-chains-tests.md §9). Still a release
# hardened and fully stripped build of every boundary, so the suite runs the
# exact packaged form used by a deployment. `make up` never calls this; the
# prefix keeps the mock set from overwriting the real binaries on the adversary.
build-attacks-mock:
	$(MAKE) build-attacks ATTACK_FEATURES=mock-scan-test-build

# Write PREBUILT/<arch>/ from a fresh build, for the public release that ships
# the attack binaries without the generator source. Captures the binaries,
# chains.json, a SHA256SUMS of them, and SOURCE_SHA256 — the review digest of the
# attack chains' source, computed by the episode runner's own function so that,
# with the source absent, the runner can confirm the binaries come from the
# source its review table was reviewed against. Run on Linux, per architecture.
write-prebuilt: $(STAMP) build-attacks
	@set -e; \
	dst="$(PREBUILT)/$(ARCH)"; \
	echo "==> write $$dst"; \
	rm -rf "$$dst"; mkdir -p "$$dst"; \
	cp $(DIST)/* "$$dst"/; \
	( cd "$$dst" && rm -f SHA256SUMS SOURCE_SHA256 && files=$$(ls -1) && sha256sum $$files > SHA256SUMS ); \
	$(PY) -c "import sys; sys.path.insert(0, '..'); from competition.episode_runner import chains; print(chains.source_digest())" > "$$dst/SOURCE_SHA256"; \
	grep -Eqx '[0-9a-f]{64}' "$$dst/SOURCE_SHA256" || { \
		echo "$$dst/SOURCE_SHA256 does not hold a review digest of 64 hexadecimal characters" >&2; \
		exit 1; }; \
	echo "wrote $$(ls -1 "$$dst" | wc -l | tr -d ' ') files to $$dst"

# Stage every binary and its chains.json manifest in ATTACK_SRC/dist onto a
# running adversary launch host, without rebuilding its image. The image already
# holds them from its build; this replaces them after a new `make build-attacks`.
# Binaries land in ATTACK_DEST with mode 0755.
push-attacks: require-incus
	@bins="$$(find $(ATTACK_SRC)/dist -maxdepth 1 -type f ! -name chains.json ! -name SHA256SUMS ! -name SOURCE_SHA256 2>/dev/null)"; \
	test -n "$$bins" || { \
		echo "$(ATTACK_SRC)/dist is empty or absent — run 'make build-attacks' first" >&2; \
		exit 1; }; \
	for b in $$bins; do \
		file "$$b" | grep -q ELF || { \
			echo "$$b is not a Linux ELF binary — did 'make build-attacks' run on macOS?" >&2; \
			echo "Rebuild it inside the Lima VM before pushing." >&2; \
			exit 1; }; \
		file -b "$$b" | grep -q ', stripped' || { \
			echo "$$b is not stripped — refusing to deploy a non-hardened attack binary" >&2; \
			exit 1; }; \
		echo "==> push $$b -> $(ADVERSARY):$(ATTACK_DEST)/$$(basename $$b)"; \
		incus file push "$$b" "$(ADVERSARY)$(ATTACK_DEST)/$$(basename $$b)" --mode 0755; \
	done; \
	manifest="$(ATTACK_SRC)/dist/chains.json"; \
	test -f "$$manifest" || { echo "$$manifest is missing — rebuild the attacks" >&2; exit 1; }; \
	echo "==> push $$manifest -> $(ADVERSARY):$(ATTACK_DEST)/chains.json"; \
	incus file push "$$manifest" "$(ADVERSARY)$(ATTACK_DEST)/chains.json" --mode 0644

clean:
	rm -rf build/

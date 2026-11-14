all: diagrams/network.png

diagrams/network.png: diagrams/network.mmd .puppeteer.json
	mmdc -p .puppeteer.json -i $< -o $@ --scale 3 --backgroundColor white

.puppeteer.json:
	@echo "Missing .puppeteer.json — create it with the path to your Chromium binary:\n"
	@echo "  npx puppeteer browsers install chrome-headless-shell"
	@echo "\nThen create .puppeteer.json containing:\n"
	@echo '  { "executablePath": "/path/to/chrome-headless-shell" }'
	@echo ""
	@exit 1

clean:
	rm -f diagrams/network.png

# ── Deployment / node access ──────────────────────────────────────────────────
# These operate on the deployed Incus network, which the deploy/ Makefile owns
# (it knows the instance names and holds the incus invocations). Forwarding them
# here keeps the repository root the single entry point: `make ssh NODE=fms-01`,
# `make logs NODE=fms-01`, `make build-attacks`, and `make push-attacks` work
# from the root as well as from deploy/.
ssh logs build-attacks push-attacks:
	$(MAKE) -C deploy $@ NODE=$(NODE)

# ── Test suites ──────────────────────────────────────────────────────────────
# `make test` runs every suite; run it inside the Lima test VM, which provisions
# PostgreSQL and Incus. On a dev machine run `make test-local`, which runs only
# the suites needing neither — the rest are gated on their VM-provided services.
# `make test-<suite>` runs just one.
#
#   test-sim          Sim (infra/sim) cargo tests; self-contained, no fixtures.
#   test-hmi          HMI (hmi) cargo tests; self-contained, no fixtures.
#   test-sim-hmi-fms  Sim<->HMI<->FMS three-way cycle; self-contained (the real
#                     sim, HMI, and FMS in one process against an embedded
#                     PostgreSQL), needs neither Incus nor a provided database.
#   test-fms-unit     FMS unit tests only (src modules); no database needed.
#   test-fms          FMS unit + API tests; starts a throwaway Postgres for the
#                     api.rs integration tests (Postgres comes from the Lima VM).
#   test-deploy       Renderer unit tests + deployed-network integration.
#   test-integration  FMS / corp-it integration against the deployment.
#   test-attack       Hardened attack binaries exercised against the deployment.
#   test-hist-harness Log-server harness pins; self-contained, no fixtures.
#   test-hist         Log-server full suite against the deployment.
#   test-siem         Security monitoring full suite against the deployment.
#
# Dependency tiers, easiest first:
#   test-sim, test-hmi, test-fms-unit, — Rust toolchain only (no Incus and no
#   test-sim-hmi-fms                      provided Postgres; the three-way test
#                                        brings its own embedded database). These
#                                        are what `make test-local` runs.
#   test-fms                           — needs PostgreSQL, provided by the Lima
#                                        test VM (not intended to run on a dev
#                                        machine; use test-fms-unit there).
#   test-deploy, test-integration,     — need a deployed Incus network
#   test-attack, test-hist, test-siem    (`make -C deploy up`), also from the VM;
#                                        integration also needs the slapd stub.
# `make test` runs them in that order and continues past failures, so the
# self-contained suites still report even when the VM services are absent.

.DEFAULT_GOAL := test
PG_SCRIPT     := fms/tests/start-test-postgres.sh

# Run every suite, continuing past a failure so all results show, but exit
# non-zero if any failed. Ordered easiest-first (self-contained cargo suites,
# then Postgres, then the deployment-dependent suites) so a missing
# Incus/Postgres never blocks the suites that do not need it.
test:
	@rc=0; \
	$(MAKE) test-sim || rc=$$?; \
	$(MAKE) test-hmi || rc=$$?; \
	$(MAKE) test-sim-hmi-fms || rc=$$?; \
	$(MAKE) test-fms || rc=$$?; \
	$(MAKE) test-deploy || rc=$$?; \
	$(MAKE) test-integration || rc=$$?; \
	$(MAKE) test-attack || rc=$$?; \
	$(MAKE) test-hist || rc=$$?; \
	$(MAKE) test-siem || rc=$$?; \
	exit $$rc

# Only the suites that need neither Incus nor Postgres — the sim and HMI cargo
# suites, the FMS *unit* tests (not its Postgres-backed api.rs), and the deploy
# renderer unit tests. Runs on a bare host (e.g. macOS) with just the Rust
# toolchain and python3-venv.
test-local:
	@rc=0; \
	$(MAKE) test-sim || rc=$$?; \
	$(MAKE) test-hmi || rc=$$?; \
	$(MAKE) test-sim-hmi-fms || rc=$$?; \
	$(MAKE) test-fms-unit || rc=$$?; \
	$(MAKE) -C deploy test-unit || rc=$$?; \
	$(MAKE) test-hist-harness || rc=$$?; \
	exit $$rc

# sim and hmi are self-contained cargo suites with no external fixtures, so each
# runs its crate's tests directly (mirroring test-fms without the Postgres step).
# --no-fail-fast runs every test binary even when one fails, so a single failure
# does not hide the rest of the suite's results.
test-sim:
	cargo test --manifest-path infra/sim/Cargo.toml --no-fail-fast

test-hmi:
	cargo test --manifest-path hmi/Cargo.toml --no-fail-fast

# The sim<->HMI<->FMS three-way integration test is self-contained: the real
# sim, HMI, and FMS run in one process against an embedded PostgreSQL, needing
# neither Incus nor the Lima-provided Postgres. It lives under tests/integration
# but runs here as its own tier-1 suite so it reports on a bare host rather than
# being gated behind the incus-dependent integration `setup`.
test-sim-hmi-fms:
	$(MAKE) -C tests/integration test-sim-hmi-fms

# FMS unit tests only: the src #[cfg(test)] modules, compiled into the lib
# target, which need no database. `--lib` excludes the api.rs integration tests
# (a separate test target) that require Postgres. SQLX offline mode lets the
# crate compile without a live database (uses the checked-in fms/.sqlx data).
test-fms-unit:
	SQLX_OFFLINE=true cargo test --lib --manifest-path fms/Cargo.toml --no-fail-fast

# fms is self-contained: start a throwaway Postgres for the api.rs integration
# tests, run cargo test, always stop it, and exit with cargo's status.
test-fms:
	@if bash $(PG_SCRIPT); then \
		SQLX_OFFLINE=true cargo test --manifest-path fms/Cargo.toml --no-fail-fast; status=$$?; \
		bash $(PG_SCRIPT) --stop; exit $$status; \
	else exit 1; fi

test-deploy:
	$(MAKE) -C deploy test

test-integration:
	$(MAKE) -C tests/integration test

# Attack-chain live suite: run the staged attack task binaries on the adversary
# host and verify each effect from the RavensPort side. Needs a deployed network
# with the binaries already built and staged (make build-attacks in the VM, then
# make push-attacks). See specs/attack_chains/attack-chains-tests.md.
test-attack:
	$(MAKE) -C tests/integration test-attack

# Log-server (hist) suite, placed in tests/integration (mirroring
# test-sim-hmi-fms)
test-hist-harness:
	$(MAKE) -C tests/integration test-hist-harness

test-hist:
	$(MAKE) -C tests/integration test-hist

# Security monitoring (siem) suite, placed in tests/integration (mirroring
# test-hist).
test-siem:
	$(MAKE) -C tests/integration test-siem

.PHONY: all clean test test-local test-fms test-fms-unit test-sim test-hmi test-sim-hmi-fms test-deploy test-integration test-hist-harness test-hist test-siem test-attack ssh logs build-attacks push-attacks

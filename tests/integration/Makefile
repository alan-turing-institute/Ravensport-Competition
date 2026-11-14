# Integration test suite — the FMS service and corp-it LDAP server, driven
# against a deployed incus network, plus the self-contained sim<->HMI<->FMS
# three-way integration test.
#
# Typical first-time setup and run (runs every deployment-gated test below):
#   make test
#
# Can be invoked from the repository root:
#   make -f tests/integration/Makefile help
#
# -- AGENTS / CI, READ THIS BEFORE RUNNING ----------------------------------
# Every `make test` target here needs the full incus VM environment: the FMS
# (`test-fms-*`) and corp-it (`test-corp-it`) targets drive a deployed `fms-01`
# over the network and configure host routes, so they CANNOT run on a plain dev
# box and `make test` will fail at `setup`.
#
# `test-fms-*` creates jobs against the same fms-01 the deployed simulator's
# dock operator drives, so it can lose the FMS's single job slot to the
# simulator. Losing shows up as a pytest *skip*, not a failure (see the
# "Exclusive-use handling" section below) — a `test-fms-*` run with skipped
# tests is not a broken run, but re-run it once the simulator is between jobs
# before treating its results as full coverage.
#
# The sim<->HMI<->FMS test is the exception and is deliberately NOT part of
# `make test` here. It is fully self-contained — the real sim, HMI and FMS run
# together in one process against an embedded PostgreSQL, needing NEITHER incus
# NOR Docker — so the repository-root Makefile runs it as its own tier-1 suite
# (part of `make test-local`). To run it directly from this directory:
#   make test-sim-hmi-fms
# ---------------------------------------------------------------------------

# Both paths stay relative to the directory make runs in, "./" under
# `make -C tests/integration` and "tests/integration/" under
# `make -f tests/integration/Makefile`, so that they hold no space even when the
# checkout's path does. make's own path functions, such as $(abspath), split a
# path at its spaces, and a target or prerequisite cannot hold a space.
THIS_DIR  := $(dir $(lastword $(MAKEFILE_LIST)))
REPO_ROOT := $(THIS_DIR)../..

INCUS  ?= incus
CARGO  ?= cargo

# Python test tooling runs from a project-local virtualenv (mirroring
# deploy/Makefile) so pytest and its dependencies stay off the system
# interpreter — no PEP 668 `--break-system-packages` override. PYTHON is only
# the bootstrap interpreter used to create the venv. The platform suffix keeps
# the macOS host and the Linux VM from sharing one venv over the Lima home mount
# (see deploy/Makefile for the full explanation).
PYTHON ?= python3
VENV   := $(THIS_DIR).venv-$(shell uname -s)-$(shell uname -m)
STAMP  := $(VENV)/.stamp-integration
PIP    := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest

# Network addresses — match topology.yaml and conftest.py defaults.
RUNNER_HOST    ?= 10.40.0.1
FMS_HOST       ?= 10.40.0.10
CORP_IT_HOST   ?= 10.30.0.20
CORP_IT_LDAP_HOST ?= corp-it
CORP_IT_LDAPS_PORT ?= 636
FW_OT_DMZ_HOST ?= 10.40.0.2

# Stub listen ports — match conftest.py defaults.
ATG_STUB_PORT      ?= 10001
MODBUS_STUB_PORT   ?= 5020
VMAGENT_STUB_PORT  ?= 4029
VLAGENT_STUB_PORT  ?= 4129
SYSLOG_STUB_PORT   ?= 4514
LDAP_STUB_PORT     ?= 1389

# The FMS verifies the syslog listener's certificate against the host name it
# dials (fms.md §3), and certificates carry a DNS SAN only, never an IP SAN
# (digital.md §2.7). The stub is therefore reached by name: configure-fms maps
# SYSLOG_STUB_NAME to RUNNER_HOST in fms-01's /etc/hosts, and stub-tls issues
# the stub a certificate for that name from the deployment's CA, which fms-01's
# system trust store already holds.
SYSLOG_STUB_NAME   ?= log-server-stub
CA_DIR             ?= $(REPO_ROOT)/deploy/build/ca
STUB_TLS_DIR        = $(THIS_DIR).stub-tls
SYSLOG_STUB_CERT    = $(STUB_TLS_DIR)/$(SYSLOG_STUB_NAME).pem
SYSLOG_STUB_KEY     = $(STUB_TLS_DIR)/$(SYSLOG_STUB_NAME).key

# Log-server (hist) suite — node names and addresses match topology.yaml and
# hist/conftest.py defaults; override as needed. Ports default in conftest.py.
HIST_NODE_NAME     ?= hist-01
HIST_DMZ_NODE_NAME ?= hist-dmz-01
HIST_HOST          ?= 10.40.0.11
HIST_DMZ_HOST      ?= 10.20.0.20
HIST_HMI_NODE      ?= hmi-01
HIST_GRAFANA_PORT  ?= 443

# Reconciliation workstation (recon) suite — node names and addresses match
# topology.yaml and recon/conftest.py defaults; override as needed.
RECON_NODE_NAME    ?= recon-01
RECON_HOST         ?= 10.30.0.10
RECON_PEER_NODE    ?= corp-it
RECON_SIM_HOST     ?= 10.30.0.11

# Security monitoring (siem) suite — node names and addresses match
# topology.yaml and siem/conftest.py defaults; override as needed.
SIEM_NODE_NAME     ?= siem-01
SIEM_HOST          ?= 10.30.0.30
SIEM_PEER_NODE     ?= corp-it
SIEM_INJECTOR_HOST ?= 10.20.0.10

PYTEST_OPTS ?= -v --tb=short
TEST_PATH    = $(THIS_DIR)fms
LDAP_SCRIPT  = $(THIS_DIR)fms/stubs/start-test-ldap.sh
CERT_SCRIPT  = $(THIS_DIR)fms/stubs/issue-stub-cert.sh

# Log-server (hist) suite paths. HIST_HARNESS_TESTS are the §0 self-contained
# tests HIST_PATH is the full §0–§7 suite whose §1–§7 modules drive a deployed
# hist-01 / hist-dmz-01.
HIST_PATH          = $(THIS_DIR)hist
HIST_HARNESS_TESTS = $(HIST_PATH)/test_00_pusher_wire_format.py $(HIST_PATH)/test_00_query_client.py

# Reconciliation workstation suite. Every assertion is made through incus exec,
# so unlike the hist suite this needs no `configure-` target putting the runner
# on a zone (recon/conftest.py).
RECON_PATH         = $(THIS_DIR)recon
ADMIN_PATH         = $(THIS_DIR)admin
# The operational and the competition images of siem-01 (episodes-tests.md
# §12). The suite builds images, needs incus but no deployment, and takes
# minutes, so it is not part of `test`.
COMPETITION_IMAGE_PATH = $(THIS_DIR)competition_image
# One benign episode of the episode runner's first version (episodes-tests.md
# §0.6, the first version). The suite deploys and resets, needs incus, the
# images and no running deployment, and takes about 15 minutes, so it is not
# part of `test`.
EPISODES_PATH = $(THIS_DIR)episodes
# Living ports and their forks (episodes-tests.md §0.6, what waits): Track A in
# forks of living ports in training mode, test_01_track_a_in_forks.py, and the
# draws of two entries' living ports with one living port seed,
# test_02_same_seed.py. The suite runs on the host that runs the living ports,
# with the manager's hypervisor and a frozen base disk, which the
# RAVENSPORT_LIVING_* variables of tests/integration/living/conftest.py name.
# test_01 takes 20 to 45 minutes and test_02 40 to 45, as many living ports at
# once as RAVENSPORT_LIVING_PARALLEL allows, so the suite is not part of `test`.
# LIVING_TEST names one module to run alone, such as test_02_same_seed.py.
LIVING_PATH = $(THIS_DIR)living
LIVING_TEST ?=

# sim<->HMI<->FMS three-way integration test (Rust/cargo; self-contained, needs
# neither incus nor Docker — embedded PostgreSQL only).
SIM_HMI_FMS_DIR  = $(THIS_DIR)sim-hmi-fms
SIM_HMI_FMS_OPTS ?= -- --nocapture

# Environment passed to every pytest invocation.
# PYTHONPATH is set to the repo root so that absolute imports
# (tests.integration.fms.*) resolve correctly.
TEST_ENV = \
	PYTHONPATH="$(REPO_ROOT)" \
	FMS_HOST=$(FMS_HOST) \
	RUNNER_HOST=$(RUNNER_HOST) \
	CORP_IT_HOST=$(CORP_IT_HOST) \
	ATG_STUB_PORT=$(ATG_STUB_PORT) \
	MODBUS_STUB_PORT=$(MODBUS_STUB_PORT) \
	VMAGENT_STUB_PORT=$(VMAGENT_STUB_PORT) \
	VLAGENT_STUB_PORT=$(VLAGENT_STUB_PORT) \
	SYSLOG_STUB_PORT=$(SYSLOG_STUB_PORT) \
	SYSLOG_STUB_CERT="$(SYSLOG_STUB_CERT)" \
	SYSLOG_STUB_KEY="$(SYSLOG_STUB_KEY)" \
	LDAP_STUB_PORT=$(LDAP_STUB_PORT)

# Environment for the hist suite. PYTHONPATH resolves the absolute
# tests.integration.hist.* imports; the HIST_* vars override conftest defaults.
HIST_TEST_ENV = \
	PYTHONPATH="$(REPO_ROOT)" \
	HIST_NODE_NAME=$(HIST_NODE_NAME) \
	HIST_DMZ_NODE_NAME=$(HIST_DMZ_NODE_NAME) \
	HIST_HOST=$(HIST_HOST) \
	HIST_DMZ_HOST=$(HIST_DMZ_HOST) \
	HIST_HMI_NODE=$(HIST_HMI_NODE) \
	HIST_GRAFANA_PORT=$(HIST_GRAFANA_PORT)

RECON_TEST_ENV = \
	PYTHONPATH="$(REPO_ROOT)" \
	RECON_NODE_NAME=$(RECON_NODE_NAME) \
	RECON_HOST=$(RECON_HOST) \
	RECON_PEER_NODE=$(RECON_PEER_NODE) \
	RECON_SIM_HOST=$(RECON_SIM_HOST) \
	HIST_DMZ_HOST=$(HIST_DMZ_HOST) \
	HIST_NODE_NAME=$(HIST_NODE_NAME) \
	HIST_HOST=$(HIST_HOST) \
	CORP_IT_HOST=$(CORP_IT_HOST)

# Attack-chain live suite (attack-chains-tests.md). Runs the emitted attack task
# binaries on the adversary host and verifies each effect from the RavensPort
# side. Reads everything through incus exec, so — like the recon suite — it joins
# no zone and has no configure- step. The binaries must already be built and
# staged (make build-attacks in the Lima VM, then make push-attacks); this
# target does not build or push them.
ADVERSARY_NODE   ?= adversary
ATTACK_BIN_DIR   ?= /root
ATTACK_PATH       = $(THIS_DIR)attack

ATTACK_TEST_ENV = \
	PYTHONPATH="$(REPO_ROOT)" \
	ADVERSARY_NODE=$(ADVERSARY_NODE) \
	ATTACK_BIN_DIR=$(ATTACK_BIN_DIR) \
	FMS_NODE_NAME=fms-01 \
	SIM_NODE_NAME=sim-host \
	CORP_IT_NODE_NAME=corp-it \
	CORP_IT_LDAP_HOST=$(CORP_IT_LDAP_HOST) \
	CORP_IT_LDAPS_PORT=$(CORP_IT_LDAPS_PORT)

# Environment for the siem suite. PYTHONPATH resolves the absolute
# tests.integration.siem.* imports; the SIEM_* vars override conftest defaults.
SIEM_TEST_ENV = \
	PYTHONPATH="$(REPO_ROOT)" \
	SIEM_NODE_NAME=$(SIEM_NODE_NAME) \
	SIEM_HOST=$(SIEM_HOST) \
	SIEM_PEER_NODE=$(SIEM_PEER_NODE) \
	SIEM_INJECTOR_HOST=$(SIEM_INJECTOR_HOST)

.PHONY: help install stub-tls start-ldap stop-ldap configure-fms configure-corp-it configure-hist configure-siem restart-fms teardown teardown-fms setup test-fms-fast test-fms-full test-corp-it test-sim-hmi-fms test-hist-harness test-hist test-recon test-admin test-competition-image test-episodes test-living test-siem test require-incus test-attack

help:
	@echo "Integration test suite — FMS service and corp-it LDAP server"
	@echo ""
	@echo "Targets:"
	@echo "  install            Install Python test dependencies"
	@echo "  stub-tls           Issue the syslog stub's certificate from the deployment CA"
	@echo "  start-ldap         Start the slapd stub on 0.0.0.0:LDAP_STUB_PORT"
	@echo "  stop-ldap          Stop the slapd stub"
	@echo "  configure-fms      Configure fms-01 for test mode (writes env files, restarts services)"
	@echo "  configure-corp-it  Add route to vnet-it via fw-ot-dmz so the runner can reach corp-it"
	@echo "  configure-hist     Join vnet-ot and add route to vnet-dmz so the runner can reach the log servers"
	@echo "  setup              install + start-ldap + configure-fms + configure-corp-it"
	@echo "  test               setup + test-fms-fast + test-fms-full + test-corp-it + teardown"
	@echo "                     (run the complete test suite; teardown always runs, even on failure)"
	@echo "  teardown           Undo setup: remove FMS test overrides, stop LDAP, release host network access"
	@echo "  teardown-fms       Remove /etc/fms/test-overrides.env and restart the FMS only"
	@echo "  test-fms-fast      FMS groups 1-3, 5, 9  (no boundary-protocol stubs required)"
	@echo "  test-fms-full      All FMS groups  (all boundary-protocol stubs required)"
	@echo "  test-corp-it       corp-it LDAP server verification tests (standalone)"
	@echo "  test-hist-harness  log-server §0 harness pins (self-contained; no incus)"
	@echo "  test-hist          log-server §0-§7 full suite (needs deployment)"
	@echo "  test-recon         reconciliation workstation §2-§3 image level, §6-§8 (needs deployment)"
	@echo "  test-attack        attack-chain live suite: run staged attack binaries, verify effects (needs deployment)"
	@echo "  test-admin         systems-administration SSH path (needs deployment)"
	@echo "  test-competition-image  operational and competition images of siem-01 (builds images; not part of 'test')"
	@echo "  test-episodes      a benign and an adversarial suppressed episode and four Track A episodes, each from deployment to reset (no running deployment; not part of 'test')"
	@echo "  test-living        Track A in forks, and one seed for two entries (LIVING_TEST=<module> runs one; host with the living port's hypervisor and base; not part of 'test')"
	@echo "  test-siem          security monitoring §0-§8 full suite (needs deployment + a runner on vnet-ext)"
	@echo "  test-sim-hmi-fms   sim<->HMI<->FMS three-way cycle (self-contained; not part of 'test')"
	@echo ""
	@echo "Variables (current values):"
	@printf "  %-30s %s\n" "RUNNER_HOST=$(RUNNER_HOST)" "host bridge IP on vnet-ot"
	@printf "  %-30s %s\n" "FMS_HOST=$(FMS_HOST)" "fms-01 IP on vnet-ot"
	@printf "  %-30s %s\n" "CORP_IT_HOST=$(CORP_IT_HOST)" "corp-it IP on vnet-it"
	@printf "  %-30s %s\n" "FW_OT_DMZ_HOST=$(FW_OT_DMZ_HOST)" "fw-ot-dmz IP on vnet-ot (next hop to vnet-it)"
	@printf "  %-30s %s\n" "ATG_STUB_PORT=$(ATG_STUB_PORT)" ""
	@printf "  %-30s %s\n" "MODBUS_STUB_PORT=$(MODBUS_STUB_PORT)" ""
	@printf "  %-30s %s\n" "VMAGENT_STUB_PORT=$(VMAGENT_STUB_PORT)" "vmagent import endpoint stub"
	@printf "  %-30s %s\n" "VLAGENT_STUB_PORT=$(VLAGENT_STUB_PORT)" "vlagent JSON-line endpoint stub"
	@printf "  %-30s %s\n" "SYSLOG_STUB_PORT=$(SYSLOG_STUB_PORT)" "vlagent syslog-over-TLS listener stub"
	@printf "  %-30s %s\n" "SYSLOG_STUB_NAME=$(SYSLOG_STUB_NAME)" "name the stub's certificate is issued for"
	@printf "  %-30s %s\n" "LDAP_STUB_PORT=$(LDAP_STUB_PORT)" "389 requires root; use 1389 for non-root"
	@echo ""
	@echo "Examples:"
	@echo "  make test                                   # complete suite with setup"
	@echo "  make setup test-fms-fast                    # fast FMS suite with defaults"
	@echo "  make test-fms-full                          # full FMS suite (re-run without setup)"
	@echo "  make start-ldap LDAP_STUB_PORT=389          # run slapd as root on standard port"
	@echo "  make test-corp-it                           # corp-it LDAP tests (standalone)"
	@echo "  make test-sim-hmi-fms                        # three-way cycle (no incus/Docker needed)"

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

install: $(STAMP)

# Rebuilt whenever requirements.txt changes; keeps pytest and its dependencies
# off the system interpreter (no PEP 668 override).
$(STAMP): $(THIS_DIR)requirements.txt
	$(PYTHON) -m venv "$(VENV)"
	"$(PIP)" install --upgrade pip
	"$(PIP)" install -r "$(THIS_DIR)requirements.txt"
	touch "$(STAMP)"

# ---------------------------------------------------------------------------
# Syslog stub certificate (see stubs/issue-stub-cert.sh). Re-issued on every
# run, so a rebuilt CA or an expired leaf never reaches the FMS.
# ---------------------------------------------------------------------------

stub-tls:
	bash "$(CERT_SCRIPT)" "$(CA_DIR)" "$(STUB_TLS_DIR)" "$(SYSLOG_STUB_NAME)"

# ---------------------------------------------------------------------------
# slapd stub for FMS tests
# ---------------------------------------------------------------------------

start-ldap:
	LDAP_STUB_PORT=$(LDAP_STUB_PORT) bash "$(LDAP_SCRIPT)"

stop-ldap:
	bash "$(LDAP_SCRIPT)" --stop

# Restart fms.service on fms-01 and fail loudly if it does not reach active
# within ~10s, so a missing binary or bad env file surfaces here rather than 60s
# later as a generic atg_reading timeout. fms-serial.service was retired (see
# deploy/tests/unit/test_topology.py), so only fms.service is restarted.
RESTART_FMS = $(INCUS) exec fms-01 -- bash -c 'systemctl restart fms.service; for _ in $$(seq 1 20); do [ "$$(systemctl is-active fms.service)" = active ] && exit 0; sleep 0.5; done; echo "fms.service did not reach active state on fms-01" >&2; systemctl status --no-pager fms.service >&2 || true; exit 1'

# ---------------------------------------------------------------------------
# Configure fms-01 for test mode
#
# Assigns the runner host IP to the vnet-ot bridge (if not already present)
# so the test runner can reach fms-01 and fms-01 can reach the stub servers.
#
# Writes /etc/fms/test-overrides.env on fms-01 via incus exec, pointing all
# FMS boundary connections at the test runner's stubs with fast poll intervals,
# then restarts fms.service.  Safe to re-run; later invocations overwrite the
# file.
#
# Also adds a SYSLOG_STUB_NAME entry to fms-01's /etc/hosts. teardown-fms
# restores the file from /etc/hosts.ravensport, as ravensport-hosts.service
# does at every boot, so the entry does not survive either.
# ---------------------------------------------------------------------------

configure-fms: stub-tls
	sudo ip addr add $(RUNNER_HOST)/24 dev vnet-ot 2>/dev/null || true
	$(INCUS) exec fms-01 -- bash -c '\
	    cp /etc/hosts.ravensport /etc/hosts && \
	    printf "%s %s\n" "$(RUNNER_HOST)" "$(SYSLOG_STUB_NAME)" >> /etc/hosts'
	$(INCUS) exec fms-01 -- bash -c '\
	    mkdir -p /etc/fms && \
	    { printf "%s\n" \
	          "FMS_ATG_HOST=$(RUNNER_HOST)" \
	          "FMS_ATG_PORT=$(ATG_STUB_PORT)" \
	          "FMS_DISPENSER_HOST=$(RUNNER_HOST)" \
	          "FMS_DISPENSER_PORT=$(MODBUS_STUB_PORT)" \
	          "FMS_VMAGENT_URL=http://$(RUNNER_HOST):$(VMAGENT_STUB_PORT)/api/v1/import" \
	          "FMS_VLAGENT_JSONLINE_URL=http://$(RUNNER_HOST):$(VLAGENT_STUB_PORT)/insert/jsonline" \
	          "FMS_VLAGENT_SYSLOG_HOST=$(SYSLOG_STUB_NAME)" \
	          "FMS_VLAGENT_SYSLOG_PORT=$(SYSLOG_STUB_PORT)" \
	          "FMS_ATG_POLL_INTERVAL=1" \
	          "FMS_MODBUS_POLL_INTERVAL=1" \
	          "FMS_LDAP_URI=ldap://$(RUNNER_HOST):$(LDAP_STUB_PORT)" \
	          "FMS_SESSION_TIMEOUT=5" \
	          "FMS_VESSEL_RFID_HOST=$(RUNNER_HOST)"; \
		} > /etc/fms/test-overrides.env'
	$(RESTART_FMS)
	@echo "fms-01 configured for test mode (runner: $(RUNNER_HOST))."

# ---------------------------------------------------------------------------
# FMS restart (safe to call without re-running configure-fms)
# ---------------------------------------------------------------------------

restart-fms:
	$(RESTART_FMS)
	@echo "fms-01 services restarted."

# ---------------------------------------------------------------------------
# Configure the test runner's route to vnet-it
#
# Adds a host route so the test runner (on vnet-ot) can reach corp-it (on
# vnet-it) via the same two-hop firewall path the FMS uses in production:
#   vnet-ot → fw-ot-dmz (FW_OT_DMZ_HOST) → fw-dmz-it → vnet-it.
# Safe to re-run; the || true suppresses the error if the route already exists.
# ---------------------------------------------------------------------------

configure-corp-it:
	sudo ip addr add $(RUNNER_HOST)/24 dev vnet-ot 2>/dev/null || true
	sudo ip route add 10.30.0.0/24 via $(FW_OT_DMZ_HOST) dev vnet-ot 2>/dev/null || true
	@echo "Route to vnet-it (10.30.0.0/24) via $(FW_OT_DMZ_HOST) in place."

# ---------------------------------------------------------------------------
# Log-server runner access
#
# Puts the runner on vnet-ot as an ordinary OT host — the same line
# configure-fms and configure-corp-it already run. That is all the hist suite
# needs over the network: the FMS simulator pushes to hist-01 on 8429, 9429 and
# 514 as a real client, and hist-01 is then a same-zone peer.
#
# No route to any other zone, so the host reaches none. Everything across a
# boundary is read through incus exec instead (hist/conftest.py), including
# hist-dmz-01's Grafana, which 443 into the DMZ admits only from vnet-it.
#
# Safe to re-run; the || true suppresses the error if the address is present.
# ---------------------------------------------------------------------------

configure-hist:
	sudo ip addr add $(RUNNER_HOST)/24 dev vnet-ot 2>/dev/null || true
	@echo "Runner on vnet-ot as $(RUNNER_HOST); log servers in the DMZ are read via incus exec."

# ---------------------------------------------------------------------------
# Security monitoring runner access
#
# Puts the runner on vnet-ext — the one suite in the repository that needs a
# presence on the external zone, because the TLS-protocol boundary-inspection
# tests (siem-tests.md §0.5, §5) must exercise the real network path Suricata
# inspects rather than substitute incus exec. Everything else in the suite
# reads through incus exec and needs no runner placement at all.
#
# Safe to re-run; the || true suppresses the error if the address is present.
# ---------------------------------------------------------------------------

SIEM_EXT_RUNNER_HOST ?= 10.10.0.1
FW_EXT_DMZ_HOST       ?= 10.10.0.2

configure-siem:
	sudo ip addr add $(SIEM_EXT_RUNNER_HOST)/24 dev vnet-ext 2>/dev/null || true
	sudo ip route add 10.20.0.0/24 via $(FW_EXT_DMZ_HOST) dev vnet-ext 2>/dev/null || true
	@echo "Runner on vnet-ext as $(SIEM_EXT_RUNNER_HOST); route to vnet-dmz (10.20.0.0/24) via $(FW_EXT_DMZ_HOST) in place for the boundary-inspection tests; everything else in the siem suite is read via incus exec."

# ---------------------------------------------------------------------------
# Composite setup
# ---------------------------------------------------------------------------

# Fail early with a clear message if Incus is unavailable (shared with the
# deploy Makefile), rather than dying on a raw `incus` error partway through.
require-incus:
	bash "$(REPO_ROOT)/deploy/scripts/require-incus.sh"

# The FMS binary is baked into the fms-01 image at build time (fms-01.sh.j2)
# and fms.service is enabled, so no separate binary push is needed after make up.
setup: require-incus install start-ldap configure-fms configure-corp-it

# ---------------------------------------------------------------------------
# Test runs
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------
#
# `configure-fms` writes /etc/fms/test-overrides.env to repoint the FMS at the
# runner's stubs, and nothing removed it. That file outliving the test run is
# not merely untidy: with it in place the FMS binds LDAP against a stub that
# stopped when pytest exited, so every operator login fails and the simulator's
# dock operator panics on the 401. It also redirects the audit trail, because
# FMS_VLAGENT_SYSLOG_HOST names the runner's stub rather than hist-01. The login
# failures are then invisible on the log server, which is the one place they are
# supposed to be recorded — the two effects arrive together, so the evidence
# disappears exactly when it is needed.
#
# teardown-fms returns the FMS to its deployed configuration. teardown also
# releases the host-side network access the configure-* targets add.

teardown-fms: require-incus
	$(INCUS) exec fms-01 -- bash -c '\
	    rm -f /etc/fms/test-overrides.env && \
	    cp /etc/hosts.ravensport /etc/hosts'
	$(RESTART_FMS)
	@echo "fms-01 returned to its deployed configuration."

teardown: teardown-fms stop-ldap
	sudo ip route del 10.30.0.0/24 via $(FW_OT_DMZ_HOST) dev vnet-ot 2>/dev/null || true
	sudo ip addr del $(RUNNER_HOST)/24 dev vnet-ot 2>/dev/null || true
	@echo "Runner removed from vnet-ot; route to vnet-it withdrawn."
	@echo ""
	@echo "Restart the simulator if it crashed while the FMS was in test mode:"
	@echo "    incus exec sim-host -- rc-service sim zap"
	@echo "    incus exec sim-host -- rc-service sim start"

# ---------------------------------------------------------------------------
# Exclusive-use handling
# ---------------------------------------------------------------------------
#
# The FMS holds a single current-job slot (narrative.md §273: no concurrent
# transactions). The simulator's dock operator drives real jobs through it, so a
# test suite that creates its own competes for that slot. Every test that
# creates a job goes through conftest.py's create_job_or_skip, which relies on
# the FMS's own atomic slot claim (fms/src/api/jobs.rs try_set_current_job): if
# the create loses the race, the FMS's 409 is turned into a pytest skip (with
# a printed reason) rather than a failure. Hence a run with the simulator active
# will (potentially) show skips here, but not failures; a run with many skips is
# worth re-running once the simulator is between jobs to confirm coverage.

# `setup` writes /etc/fms/test-overrides.env (see the comment on `teardown`
# above) and must not outlive this run, so `teardown` always fires — even if
# `setup` or a suite fails, or the run is interrupted (Ctrl-C). The `cleanup`
# trap covers both: EXIT catches ordinary completion (success or failure, via
# the closing `exit $$rc`), INT/TERM catch a signal arriving while a step is
# running. `done` guards against running teardown twice, since the INT/TERM
# handler's own `exit` re-enters the EXIT trap. Mirrors the root Makefile's
# `rc=$$?` chain (test-fms's Postgres start/stop) for the "always clean up"
# part; the trap is the addition needed to survive an interruption, which a
# plain `rc=$$?` chain does not — a signal never reaches the line after the
# one it hits.
test:
	@rc=0; done=0; \
	cleanup() { if [ "$$done" = 0 ]; then done=1; $(MAKE) teardown || echo "WARNING: teardown failed — fms-01 may still be in test mode" >&2; fi; }; \
	trap 'cleanup; exit 130' INT TERM; \
	trap 'cleanup' EXIT; \
	$(MAKE) setup || rc=$$?; \
	$(MAKE) test-fms-fast || rc=$$?; \
	$(MAKE) test-fms-full || rc=$$?; \
	$(MAKE) test-corp-it || rc=$$?; \
	$(MAKE) test-admin || rc=$$?; \
	exit $$rc

test-fms-fast: stub-tls restart-fms $(STAMP)
	$(TEST_ENV) \
	"$(PYTEST)" $(PYTEST_OPTS) -m fast "$(TEST_PATH)"

test-fms-full: stub-tls restart-fms $(STAMP)
	$(TEST_ENV) \
	"$(PYTEST)" $(PYTEST_OPTS) -m full "$(TEST_PATH)"

test-corp-it: configure-corp-it $(STAMP)
	$(TEST_ENV) \
	"$(PYTEST)" $(PYTEST_OPTS) "$(THIS_DIR)ldap"

# Log-server (hist) integration suite split by runnability
test-hist-harness: $(STAMP)
	$(HIST_TEST_ENV) \
	"$(PYTEST)" $(PYTEST_OPTS) $(HIST_HARNESS_TESTS)

test-hist: require-incus configure-hist $(STAMP)
	$(HIST_TEST_ENV) \
	"$(PYTEST)" $(PYTEST_OPTS) "$(HIST_PATH)"

# ---------------------------------------------------------------------------
# Reconciliation workstation suite
#
# recon-tests.md §5-§8 plus the image-level half of §2. Needs a deployed
# recon-01; for the reachability probe, a running corp-it; and for §5's query
# path, running hist-01 and hist-dmz-01. Reads everything through incus exec,
# seeding §5's records inside hist-01, so there is no configure- step and the
# runner joins no zone.
#
# §2's negotiation assertions and §3-§4 (the desktop session and the browser's
# behaviour) are absent: they need a client speaking Remote Desktop Protocol,
# which is not built yet.
# ---------------------------------------------------------------------------

test-recon: require-incus $(STAMP)
	$(RECON_TEST_ENV) \
	"$(PYTEST)" $(PYTEST_OPTS) "$(RECON_PATH)"

test-admin: require-incus $(STAMP)
	PYTHONPATH="$(REPO_ROOT)" \
	"$(PYTEST)" $(PYTEST_OPTS) "$(ADMIN_PATH)"

test-competition-image: require-incus $(STAMP)
	PYTHONPATH="$(REPO_ROOT)" \
	"$(PYTEST)" $(PYTEST_OPTS) "$(COMPETITION_IMAGE_PATH)"

test-episodes: require-incus $(STAMP)
	PYTHONPATH="$(REPO_ROOT)" \
	"$(PYTEST)" $(PYTEST_OPTS) "$(EPISODES_PATH)"

# The living port's suite runs on the host, which needs no incus of its own:
# Incus runs inside the living port's VM.
test-living: $(STAMP)
	PYTHONPATH="$(REPO_ROOT)" \
	"$(PYTEST)" $(PYTEST_OPTS) "$(LIVING_PATH)$(if $(LIVING_TEST),/$(LIVING_TEST))"

# ---------------------------------------------------------------------------
# Attack-chain live suite
#
# Needs a deployed network with the adversary host up. Unlike the other suites
# this one builds and stages its own binaries: it emits the TEST-ONLY mock set
# (make build-attacks-mock) and pushes it, so every binary is name-prefixed
# `mock-scan-test__` and its IT-segment scan returns hardcoded facts rather than
# running the live scan dozens of times. The real binaries that `make up` stages
# are left untouched (different names). The `scan-contract` binary then runs the
# REAL scan once and the suite refuses to trust the mocks unless they match it
# (test_00_scan_contract.py, attack-chains-tests.md §9). Reads ground truth
# through incus exec, so it joins no zone and has no configure- step.
#
# Deploy with the `attack` profile (`make up PROFILE=attack`) so the environment
# carries the test-oriented settings documented in deployment.md §7.4. The mock
# build+push must run in the Lima VM (it compiles native Linux binaries).
# ---------------------------------------------------------------------------

test-attack: require-incus $(STAMP)
	$(MAKE) -C "$(REPO_ROOT)/deploy" build-attacks-mock push-attacks
	$(ATTACK_TEST_ENV) ATTACK_BIN_PREFIX=mock-scan-test__ \
	"$(PYTEST)" $(PYTEST_OPTS) "$(ATTACK_PATH)"

# ---------------------------------------------------------------------------
# Security monitoring suite
#
# siem-tests.md §0-§8. Needs the full deployed topology, with a scenario in
# progress for the tests that assert on traffic it produces (§4, §5), and the
# runner on vnet-ext (configure-siem) for the boundary-inspection tests.
# Destructive tests (a full teardown/redeploy) are marked `slow` and gated
# behind SIEM_ALLOW_REDEPLOY=1; they do not run here by default.
# ---------------------------------------------------------------------------

SIEM_PATH = $(THIS_DIR)siem

test-siem: require-incus configure-siem $(STAMP)
	$(SIEM_TEST_ENV) \
	"$(PYTEST)" $(PYTEST_OPTS) -m "not slow" "$(SIEM_PATH)"

# ---------------------------------------------------------------------------
# sim<->HMI<->FMS three-way integration test
#
# Self-contained: the real sim, HMI, and FMS run together in one process with an
# embedded PostgreSQL. Needs no incus deployment, no Docker, and no stub setup,
# so it is independent of the targets above and runs on the dev box as well as in
# the VM. See sim-hmi-fms/README.md.
#
# Safe to run on its own when the rest of the suite cannot (no incus/Docker):
#   make test-sim-hmi-fms
# It has no prerequisites and does not invoke `setup`.
# ---------------------------------------------------------------------------

test-sim-hmi-fms:
	$(CARGO) test --manifest-path "$(SIM_HMI_FMS_DIR)/Cargo.toml" --test full_cycle --test supervision $(SIM_HMI_FMS_OPTS)

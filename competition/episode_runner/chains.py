"""The attack chains an adversarial episode may run (episodes.md §10, onset and
pacing; §15, episode mix).

The attack phase's generator writes the chain catalogue,
attack-chains/dist/chains.json, beside the binaries it builds, with
schema_version 2. Each attack artifact names a chain's binary, its foothold,
its impact stage, its stages with their requires and provides, and the
conditions that its stages declare. The catalogue interprets nothing, so the episode
runner reads three things from it:

- each stage's target, derived from the stage's requires;
- the port activity each stage needs, and whether a stage restarts the Wazuh
  manager, from the table of reviewed stages below;
- the library: the chains that an episode can run.

A chain belongs to the library when it starts on the adversary host, every
stage of it is in the table, no stage restarts the Wazuh manager, and every
port activity it needs is one that predicted_events.py predicts for each
episode: the fuel supplier's poll, or a refuel during a scan of the inventory
stage; or one that the episode runner reads from the fork: an audit that the
living port completed before the fork instant. An episode then admits the
chains of the library whose activities it holds while their stages run
(predicted_events.py). A stage missing from the table keeps its chain out, so
the table fails closed.

The stages' declared conditions name four of the port activities, but not the
fuel supplier's poll, nor a restart of the Wazuh manager, so the table holds
only for the source it was reviewed against. A digest of that source guards it:
once any Rust file of the attack chains' source changes, the episode runner
refuses adversarial episodes until the table is reviewed again against the
change. A chain that declares a condition for which the table holds no need of
its stages stays out of the library.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
ATTACK_SRC = REPO / "attack-chains"
# The attack chains' source as reviewed (episodes.md §10.1, the attack chains):
# every Rust file of the crates under ATTACK_SRC, among them the runtime crate
# that takes the main crate's modules by path, with each crate's manifest and
# lock file, which fix the release profile and the dependencies. Tests,
# benchmarks, examples, build output and the catalogue are left out.
SOURCE = ATTACK_SRC
SOURCE_MANIFESTS = frozenset({"Cargo.toml", "Cargo.lock"})
SOURCE_LEFT_OUT = frozenset({"tests", "benches", "examples", "target", "dist"})
DIST = ATTACK_SRC / "dist"
# A release without the attack chains' source holds, beside the binaries and
# the catalogue in DIST, the list of their SHA-256 digests and the review
# digest of the source they were built from (episodes.md §10.1).
DIST_DIGESTS = "SHA256SUMS"
DIST_SOURCE = "SOURCE_SHA256"
CATALOGUE = DIST / "chains.json"
SCHEMA_VERSION = 2

# The node that every foothold of the library starts from, and the directory
# in which its own image holds the binaries (episodes.md §6.5, adversary
# host).
ADVERSARY = "adversary"
BINARY_DIR = "/root"

# The footholds whose chains start on the adversary host (adversary.md §1,
# placement). A foothold outside this set keeps its chains out of the library.
ADVERSARY_FOOTHOLDS = frozenset({"external-blind", "external-informed", "it-foothold"})

# The port activities a stage can need. A stage that needs the supplier's poll
# waits for the next one. A stage that needs a refuel during a scan reads a
# connection that the inventory stage saw: the FMS's to the dispenser, or the
# HMI's to the FMS, which exist only while a tug is refuelled.
SUPPLIER_POLL = "supplier_poll"
REFUEL_DURING_FMS_SCAN = "refuel_during_fms_scan"
REFUEL_DURING_HMI_SCAN = "refuel_during_hmi_scan"
AUDIT_RUNNING = "audit_running"
AUDIT_COMPLETED = "audit_completed"

# The port activities that the episode runner reads from the fork: an audit
# that the living port completed before the fork instant, whose traces the
# reconciliation user's home directory on recon-01 then holds (fork.py). At
# fork 0 the living port has completed no audit (episodes.md §10.2, choosing
# the chain).
READ_AT_THE_FORK: frozenset[str] = frozenset({AUDIT_COMPLETED})
# The port activities that predicted_events.py predicts for each episode from
# its start state. The supplier polls on its fixed schedule, with an order
# pending or not, and refuels follow the tug arrivals. A reconciliation audit
# runs every 144 hours, so an episode rarely holds one running.
PREDICTED = frozenset({SUPPLIER_POLL, REFUEL_DURING_FMS_SCAN, REFUEL_DURING_HMI_SCAN})
# The port activity that each condition the stages declare stands for
# (attack-chains/src/library.rs, the conditions). A chain's declared condition
# must be one of the needs that the table gives its stages; the stages that
# wait for the supplier's poll declare no condition.
CONDITION_NEEDS = {
    "live-reconciliation-audit-observable": AUDIT_RUNNING,
    "completed-reconciliation-audit-in-history": AUDIT_COMPLETED,
    "active-tug-dispense-during-siem-inventory": REFUEL_DURING_FMS_SCAN,
    "operator-hmi-activity-during-siem-inventory": REFUEL_DURING_HMI_SCAN,
}

MANAGER = "SecurityMonitoringManager"


@dataclass(frozen=True)
class Review:
    """What a reviewed stage needs from the port, whether it restarts the Wazuh
    manager, and its run time in seconds, earliest to latest, as the live
    episodes measured it (None where they did not). The run time of a stage
    that waits for the supplier's poll follows the poll."""

    needs: frozenset[str] = frozenset()
    restarts_manager: bool = False
    seconds: tuple[float, float] | None = None


# Every stage of the attack phase's library, reviewed against its body in
# attack-chains/src/library.rs, and the modules it calls, at 5c30433b of
# 192-attack-chains-to-merge. A body that waits for a port activity, or reads
# its traces, is listed with it. The run times come from 20 live adversarial
# episodes, widened where one run measured them, except where a comment says that a band is derived from a body changed
# since: such a band is provisional until live episodes measure it.
_PLAIN = Review()
REVIEWED: dict[str, Review] = {
    # External segment.
    "discover-gateway": Review(seconds=(1.5, 3.0)),
    "prove-raw-frame-tx": Review(seconds=(0.0, 1.0)),
    "prove-forged-frame-honoured": Review(seconds=(0.0, 1.0)),
    # tcpdump -c 1, with no timeout, for the supplier's next poll.
    "intercept-supplier-poll": Review(needs=frozenset({SUPPLIER_POLL})),
    "resolve-supplier-mac": Review(seconds=(0.5, 2.0)),
    # Waits up to CAPTURE_WINDOW_SECS for the relay's reply to a poll.
    "capture-relay-reply": Review(needs=frozenset({SUPPLIER_POLL})),
    "exfiltrate-inventory": Review(seconds=(0.0, 50.0)),
    "inject-false-feed": _PLAIN,
    "replay-false-feed": _PLAIN,
    "starve-ot-console-feed-via-relay": _PLAIN,
    # IT segment: the directory.
    "discover-it-gateway": _PLAIN,
    "scan-it-segment": Review(seconds=(266.0, 270.0)),
    "enumerate-directory": Review(seconds=(0.0, 1.0)),
    "disclose-account-inventory": Review(seconds=(0.0, 1.0)),
    "deny-directory-availability": _PLAIN,
    "spray-operator-credential": Review(seconds=(5.0, 15.0)),
    "lock-operator-via-self-password": _PLAIN,
    "spray-manager-credential": Review(seconds=(25.0, 45.0)),
    "overwrite-operator-entry-as-manager": _PLAIN,
    "recover-reconciliation-user-credential-by-reset": _PLAIN,
    # IT segment: the reconciliation workstation and the log replica.
    "open-session-as-dock-operator": _PLAIN,
    "open-session-as-reconciliation-user": _PLAIN,
    # Polls /proc every 0.1 s for up to 12 minutes, within a 12-minute read, for
    # a running audit's command line, then reads recon-01's /etc/hosts in a
    # second desktop session to resolve the replica's hostname
    # (library.rs:2433, :2619-2630, :2537-2617).
    "harvest-grafana-credential-from-proc": Review(needs=frozenset({AUDIT_RUNNING})),
    # Reads ~/.bash_history, which only a completed audit fills, then recon-01's
    # /etc/hosts, each in a desktop session of its own. A session waits up to
    # 30 s for the clipboard channel, 6 s for the desktop and the terminal,
    # types its command at 30 ms a key, and reads for up to 12 s (library.rs
    # :2537-2617, :2632-2643; rdp/mod.rs:94-133). Derived, not measured: at
    # least the two sessions' 15 s of fixed waits and typing, and at most 110 s,
    # with 5 s allowed for each connection.
    "harvest-grafana-credential-from-history": Review(
        needs=frozenset({AUDIT_COMPLETED}), seconds=(15.0, 110.0)
    ),
    "exfiltrate-replica-telemetry": _PLAIN,
    "forge-transaction-record": _PLAIN,
    "fabricate-metric-sample": _PLAIN,
    "alter-metric-history": _PLAIN,
    "persist-grafana-service-account": _PLAIN,
    "deny-dmz-log-replica-retrieval": _PLAIN,
    # IT segment: the Wazuh manager.
    "acquire-siem-management-credential": Review(seconds=(1.0, 4.0)),
    # Scans the FMS's peers for 75 s, then finds the tank-gauge relay's agent
    # and scans its peers for 75 s, then the HMI's for 75 s
    # (library.rs:4073-4187, siem_relay_agent at :4266). Two runs on a Linux
    # host measured 226.1 s in both, inside the 225.5 to 233.5 s
    # derived from the body (predicted_events.py, SCAN_WINDOWS_S; episodes.md
    # §10.1, the competition library).
    "enumerate-siem-agent-inventory": Review(seconds=(226.1, 226.1)),
    "starve-fms-tank-feed-via-route-null": _PLAIN,
    # Null-routes the console on the relay's agent, so the supplier's own poll
    # fails; after its three retries the simulator stops (sim-impl.md, retry
    # policy), which terminates the episode.
    "deny-supplier-feed-via-relay-route-null": _PLAIN,
    "deny-ot-log-replication-via-firewall-route-null": _PLAIN,
    "deny-ot-directory-via-firewall-route-null": _PLAIN,
    "deny-fms-vessel-identity-via-route-null": _PLAIN,
    # Reads the FMS's connection to the dispenser, which exists during a refuel.
    "deny-fms-dispenser-control-via-route-null": Review(
        needs=frozenset({REFUEL_DURING_FMS_SCAN})
    ),
    "deny-operator-identity-via-route-null": _PLAIN,
    # Reads the HMI's connection to the FMS, which exists during a refuel.
    "deny-operator-control-via-route-null": Review(
        needs=frozenset({REFUEL_DURING_HMI_SCAN})
    ),
    # Load a changed manager configuration, restart the manager, then restore
    # the configuration and restart it again.
    "execute-on-siem-manager-via-command-wodle": Review(restarts_manager=True),
    "exfiltrate-operational-data-from-manager": Review(restarts_manager=True),
}

# The source that REVIEWED was reviewed against, and its digest (source_digest).
REVIEWED_SOURCE = "5c30433b of 192-attack-chains-to-merge"
REVIEWED_SOURCE_SHA256 = (
    "301611b48e28b901bade48974dd68c14ecb2ae5cec4e805b0a1d167966a26e12"
)

# The node of each role the attack phase names (attack-chains/src/fact.rs),
# and of each segment.
NODE_OF_ROLE = {
    "Supplier": "sim-host",
    "Relay": "atg-relay-01",
    "ExternalSegmentGateway": "fw-ext-dmz",
    "ItSegmentGateway": "fw-dmz-it",
    "IdentityProvider": "corp-it",
    "ReconciliationWorkstation": "recon-01",
    "DmzLogReplica": "hist-dmz-01",
    "SecurityMonitoringManager": "siem-01",
}
NETWORK_OF_ZONE = {
    "External": "vnet-ext",
    "It": "vnet-it",
    "Dmz": "vnet-dmz",
    "Ot": "vnet-ot",
}
GATEWAYS = frozenset({"ExternalSegmentGateway", "ItSegmentGateway"})
# The host over which each family of capability is held.
HOLDER = {
    "LdapDirectory": "IdentityProvider",
    "ReconciliationWorkstation": "ReconciliationWorkstation",
    "DmzLogReplicaGrafana": "DmzLogReplica",
    "SecurityMonitoring": "SecurityMonitoringManager",
}


class CatalogueError(ValueError):
    """A catalogue the episode runner cannot use."""


@dataclass(frozen=True)
class Target:
    """Where a stage acts: a host by its role, a segment, or the adversary
    host alone."""

    kind: str
    name: str

    @property
    def node(self) -> str:
        if self.kind == "host":
            return NODE_OF_ROLE[self.name]
        if self.kind == "segment":
            return NETWORK_OF_ZONE[self.name]
        return ADVERSARY

    def as_json(self) -> dict[str, str]:
        return {"kind": self.kind, "name": self.name, "node": self.node}


def derive_target(requires: list[str]) -> Target:
    """A stage's target from its requires. The rule takes, in order: the one
    host with a required service; else the one host other than a gateway with
    a required address or hardware address; else the one host a required
    capability is held over; else the required segment; else the adversary
    host. Two hosts at the level that decides make the stage ambiguous."""
    services = {
        m.group(1) for k in requires if (m := re.fullmatch(r"Service\((\w+), \w+\)", k))
    }
    addresses = {
        m.group(1) for k in requires if (m := re.fullmatch(r"(?:Ip|Mac)\((\w+)\)", k))
    } - GATEWAYS
    holders = set()
    for k in requires:
        if m := re.fullmatch(r"Grant\((\w+)\(.*\)\)", k):
            if m.group(1) not in HOLDER:
                raise CatalogueError(f"no holder for the capability in {k}")
            holders.add(HOLDER[m.group(1)])
    for level in (services, addresses, holders):
        if len(level) > 1:
            raise CatalogueError(
                f"requires {requires} name several targets: {sorted(level)}"
            )
        if level:
            role = next(iter(level))
            if role not in NODE_OF_ROLE:
                raise CatalogueError(f"no node for the role {role}")
            return Target("host", role)
    segments = [
        m.group(1) for k in requires if (m := re.fullmatch(r"Segment\((\w+)\)", k))
    ]
    if segments:
        return Target("segment", segments[0])
    return Target("adversary", ADVERSARY)


@dataclass(frozen=True)
class Stage:
    name: str
    group: str
    target: Target


@dataclass(frozen=True)
class Chain:
    """One chain of the catalogue."""

    id: str
    foothold: str
    impact: str
    stages: tuple[Stage, ...]
    unreviewed: tuple[str, ...] = field(default=())
    # The declared conditions for which the table holds no need of the stages.
    unmatched: tuple[str, ...] = field(default=())

    @property
    def needs(self) -> frozenset[str]:
        return frozenset(
            a for s in self.stages for a in REVIEWED.get(s.name, _PLAIN).needs
        )

    @property
    def restarts_manager(self) -> bool:
        return any(REVIEWED.get(s.name, _PLAIN).restarts_manager for s in self.stages)

    def exclusion(
        self, read_at_the_fork: frozenset[str] = READ_AT_THE_FORK
    ) -> str | None:
        """Why the chain stays out of the library, or None when it belongs."""
        if self.foothold not in ADVERSARY_FOOTHOLDS:
            return f"its foothold {self.foothold} starts off the adversary host"
        if self.unreviewed:
            return "its stages " + ", ".join(self.unreviewed) + " are unreviewed"
        if self.unmatched:
            return (
                "the table holds no need of its stages for its conditions "
                + ", ".join(self.unmatched)
            )
        if self.restarts_manager:
            return "it restarts the Wazuh manager"
        missing = sorted(self.needs - read_at_the_fork - PREDICTED)
        if missing:
            return "it needs " + ", ".join(missing) + ", which an episode rarely holds"
        return None

    def binary(self, dist: Path = DIST) -> Path:
        return dist / self.id

    def sha256(self, dist: Path = DIST) -> str:
        return hashlib.sha256(self.binary(dist).read_bytes()).hexdigest()

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "foothold": self.foothold,
            "impact": self.impact,
            "needs": sorted(self.needs),
            "stages": [
                {"name": s.name, "group": s.group, "target": s.target.as_json()}
                for s in self.stages
            ],
        }


def load(path: Path = CATALOGUE) -> list[Chain]:
    """The chains of the catalogue at path, one for each attack artifact. A
    support artifact holds no chain. The catalogue of a mock build is refused:
    its binaries take fixed facts in place of the scan of the IT segment."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise CatalogueError(
            f"cannot read the chain catalogue {path}: {error}"
        ) from error
    version = data.get("schema_version") if isinstance(data, dict) else None
    if version != SCHEMA_VERSION:
        raise CatalogueError(
            f"{path} has schema_version {version!r}, not {SCHEMA_VERSION}"
        )
    try:
        if data["generator"]["mock_build"] is not False:
            raise CatalogueError(
                f"{path} lists a mock build, whose binaries take fixed facts in "
                "place of the scan of the IT segment: run `make build-attacks` in "
                "deploy/"
            )
        chains = []
        for artifact in data["artifacts"]:
            if artifact["kind"] == "support":
                continue
            if artifact["kind"] != "attack":
                raise CatalogueError(
                    f"{path} lists an artifact of the kind {artifact['kind']!r}"
                )
            chains.append(_chain(artifact))
    except (KeyError, TypeError) as error:
        raise CatalogueError(
            f"{path} is no catalogue with schema_version {SCHEMA_VERSION}: {error!r}"
        ) from error
    return chains


def _chain(artifact: dict[str, Any]) -> Chain:
    """The chain of an attack artifact, with the stages that the table does
    not hold and the declared conditions for which it holds no need."""
    positions = [s["position"] for s in artifact["stages"]]
    if positions != list(range(1, len(positions) + 1)):
        raise CatalogueError(
            f"the stages of {artifact['binary']} hold the positions {positions}"
        )
    stages = tuple(
        Stage(s["name"], s["group"], derive_target(s["requires"]))
        for s in artifact["stages"]
    )
    chain = Chain(
        artifact["binary"],
        artifact["foothold"]["name"],
        artifact["impact_stage"],
        stages,
        unreviewed=tuple(s.name for s in stages if s.name not in REVIEWED),
    )
    unmatched = tuple(
        c["id"]
        for c in artifact["conditions"]
        if CONDITION_NEEDS.get(c["id"]) not in chain.needs
    )
    return replace(chain, unmatched=unmatched)


def library(
    chains: list[Chain], read_at_the_fork: frozenset[str] = READ_AT_THE_FORK
) -> list[Chain]:
    """The chains that an episode can run, once it admits them
    (predicted_events.py)."""
    return [c for c in chains if c.exclusion(read_at_the_fork) is None]


def choose(library_chains: list[Chain], rng: random.Random | None = None) -> Chain:
    """One chain of the library, each with equal chance (episodes.md §15,
    episode mix)."""
    if not library_chains:
        raise CatalogueError("the library holds no chain")
    return (rng or random.SystemRandom()).choice(library_chains)


def source_files(src: Path = SOURCE) -> list[Path]:
    """Every Rust file, manifest and lock file under src that SOURCE names,
    in the order of their paths relative to src."""
    files = []
    for path in src.rglob("*"):
        relative = path.relative_to(src)
        if any(part in SOURCE_LEFT_OUT for part in relative.parts[:-1]):
            continue
        if path.is_file() and (path.suffix == ".rs" or path.name in SOURCE_MANIFESTS):
            files.append(path)
    return sorted(files, key=lambda p: p.relative_to(src).as_posix())


def source_digest(src: Path = SOURCE) -> str:
    """The SHA-256 digest of source_files(src), each taken as its path relative
    to src and its contents, in the order of the paths."""
    digest = hashlib.sha256()
    for path in source_files(src):
        name = path.relative_to(src).as_posix().encode()
        data = path.read_bytes()
        for part in (name, data):
            digest.update(len(part).to_bytes(8, "big") + part)
    return digest.hexdigest()


def check_prebuilt(dist: Path = DIST) -> None:
    """Raise CatalogueError unless dist holds a release's binaries built from
    the source that REVIEWED was reviewed against: its SOURCE_SHA256 records
    that source's digest, and every file of dist, the catalogue among them,
    matches its digest in SHA256SUMS, with no file left out of the list
    (episodes.md §10.1, a release without the source)."""
    recorded, listed = dist / DIST_SOURCE, dist / DIST_DIGESTS
    if not recorded.is_file() or not listed.is_file():
        raise CatalogueError(
            f"no attack chains' source at {SOURCE}, and no release's binaries in "
            f"{dist}: {DIST_DIGESTS} and {DIST_SOURCE} are missing"
        )
    built_from = recorded.read_text().strip()
    if built_from != REVIEWED_SOURCE_SHA256:
        raise CatalogueError(
            f"the release's binaries were built from the source {built_from}, not "
            f"from the one whose stages were reviewed ({REVIEWED_SOURCE})"
        )
    digests: dict[str, str] = {}
    for line in listed.read_text().splitlines():
        digest, _, name = line.strip().partition(" ")
        if digest:
            digests[name.strip().lstrip("*")] = digest
    present = {
        p.relative_to(dist).as_posix()
        for p in dist.rglob("*")
        if p.is_file() and p.name not in (DIST_DIGESTS, DIST_SOURCE)
    }
    unlisted = sorted(present - set(digests))
    if unlisted:
        raise CatalogueError(
            f"the release's file {unlisted[0]} is not in {DIST_DIGESTS}"
        )
    for name, digest in sorted(digests.items()):
        path = dist / name
        if not path.is_file():
            raise CatalogueError(f"the release's file {name} is missing from {dist}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise CatalogueError(
                f"the release's file {name} differs from {DIST_DIGESTS}"
            )


def check_review(
    src: Path = SOURCE, catalogue: Path = CATALOGUE, dist: Path = DIST
) -> None:
    """Raise CatalogueError unless src is the source that REVIEWED was reviewed
    against, and the catalogue, with the binaries beside it, was built after the
    source's last change. Without the source, the binaries in dist must be a
    release's binaries built from that source (check_prebuilt)."""
    if not src.is_dir() or not source_files(src):
        check_prebuilt(dist)
        return
    digest = source_digest(src)
    if digest != REVIEWED_SOURCE_SHA256:
        raise CatalogueError(
            "the attack chains' source has changed since its stages were reviewed "
            f"({REVIEWED_SOURCE}): review REVIEWED in {Path(__file__).name} against "
            f"the change, then set REVIEWED_SOURCE_SHA256 to {digest}"
        )
    newest = max(p.stat().st_mtime for p in source_files(src))
    if catalogue.stat().st_mtime < newest:
        raise CatalogueError(
            "the binaries predate the last change to the attack chains' source: "
            "run `make build-attacks` in deploy/"
        )


def unbuilt(chains: list[Chain], dist: Path = DIST) -> list[str]:
    """The chains whose binaries are missing from dist."""
    return [c.id for c in chains if not c.binary(dist).is_file()]


def find(chains: list[Chain], chain_id: str) -> Chain:
    """The chain named chain_id."""
    for chain in chains:
        if chain.id == chain_id:
            return chain
    raise CatalogueError(f"the catalogue holds no chain {chain_id}")

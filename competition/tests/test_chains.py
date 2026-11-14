"""The attack chains an adversarial episode may run (episodes.md §10, onset and
pacing; §15, episode mix).

data/chains-sample.json holds four attack artifacts of the chain catalogue, as
the attack phase's generator writes them, with schema_version 2: a chain that
every episode can run, a chain that waits for the fuel supplier's poll, a chain
that needs a running reconciliation audit and declares it as a condition, and a
chain that restarts the Wazuh manager. A test over the whole catalogue runs when
the binaries are built.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
import warnings
from pathlib import Path

import pytest

from competition.episode_runner import chains

SAMPLE = Path(__file__).resolve().parent / "data" / "chains-sample.json"


def sample() -> list[chains.Chain]:
    return chains.load(SAMPLE)


def by_id(chain_id: str) -> chains.Chain:
    return chains.find(sample(), chain_id)


def test_each_stage_takes_its_target_from_its_requires() -> None:
    disclose = by_id("it-foothold__disclose-account-inventory")
    assert [(s.name, s.target.node) for s in disclose.stages] == [
        ("scan-it-segment", "vnet-it"),
        ("enumerate-directory", "corp-it"),
        ("disclose-account-inventory", "adversary"),
    ]
    exfiltrate = by_id("external-informed__exfiltrate-inventory")
    assert [s.target.node for s in exfiltrate.stages] == [
        "vnet-ext",
        "vnet-ext",
        "vnet-ext",
        "vnet-ext",
        "atg-relay-01",
    ]


@pytest.mark.parametrize(
    ("requires", "target"),
    [
        # A required service decides, ahead of every address.
        (["Ip(ExternalSegmentGateway)", "Service(Relay, Tls)"], ("host", "Relay")),
        # An address of a gateway is only the route to the host.
        (["Ip(ItSegmentGateway)", "Ip(DmzLogReplica)"], ("host", "DmzLogReplica")),
        (["Mac(Supplier)", "Mac(ExternalSegmentGateway)"], ("host", "Supplier")),
        # A capability is held over the host that grants it.
        (
            ["Segment(It)", "Grant(ReconciliationWorkstation(InteractiveSession))"],
            ("host", "ReconciliationWorkstation"),
        ),
        (["Segment(Ot)"], ("segment", "Ot")),
        (["LdapDirectoryAccounts"], ("adversary", "adversary")),
    ],
)
def test_the_rule_takes_the_first_level_that_names_a_target(
    requires: list[str], target: tuple[str, str]
) -> None:
    derived = chains.derive_target(requires)
    assert (derived.kind, derived.name) == target


def test_two_hosts_at_the_deciding_level_make_a_stage_ambiguous() -> None:
    with pytest.raises(chains.CatalogueError, match="several targets"):
        chains.derive_target(["Service(Relay, Tls)", "Service(IdentityProvider, Ldap)"])


def test_a_capability_without_a_holder_is_refused() -> None:
    with pytest.raises(chains.CatalogueError, match="no holder"):
        chains.derive_target(["Grant(Unknown(Administrator))"])


def test_the_library_holds_the_chains_that_every_episode_can_run() -> None:
    reasons = {c.id: c.exclusion() for c in sample()}
    assert reasons == {
        "it-foothold__disclose-account-inventory": None,
        "external-informed__exfiltrate-inventory": None,
        "it-foothold__exfiltrate-replica-telemetry-1": (
            "it needs audit_running, which an episode rarely holds"
        ),
        "it-foothold__exfiltrate-operational-data-from-manager": (
            "it restarts the Wazuh manager"
        ),
    }
    assert [c.id for c in chains.library(sample())] == [
        "it-foothold__disclose-account-inventory",
        "external-informed__exfiltrate-inventory",
    ]
    assert by_id("external-informed__exfiltrate-inventory").needs == {
        chains.SUPPLIER_POLL
    }


def test_a_chain_that_reads_what_an_audit_left_belongs_to_the_library(
    tmp_path: Path,
) -> None:
    """The episode runner reads from the fork whether the living port
    completed an audit before the fork instant, so a chain that reads what the
    audit left stays in the library, and each fork admits it or not
    (predicted_events.admit)."""
    data = json.loads(SAMPLE.read_text())
    data["artifacts"][0]["stages"][1]["name"] = (
        "harvest-grafana-credential-from-history"
    )
    path = tmp_path / "chains.json"
    path.write_text(json.dumps(data))
    chain = chains.find(chains.load(path), "it-foothold__disclose-account-inventory")
    assert chain.needs == {chains.AUDIT_COMPLETED}
    assert chain.exclusion() is None
    assert chain.exclusion(frozenset()) == (
        "it needs audit_completed, which an episode rarely holds"
    )


def test_a_stage_missing_from_the_review_keeps_its_chain_out(tmp_path: Path) -> None:
    data = json.loads(SAMPLE.read_text())
    data["artifacts"][0]["stages"][1]["name"] = "a-stage-added-later"
    path = tmp_path / "chains.json"
    path.write_text(json.dumps(data))
    chain = chains.find(chains.load(path), "it-foothold__disclose-account-inventory")
    assert chain.unreviewed == ("a-stage-added-later",)
    assert chain.exclusion() == "its stages a-stage-added-later are unreviewed"


def test_a_foothold_off_the_adversary_host_keeps_its_chain_out(tmp_path: Path) -> None:
    data = json.loads(SAMPLE.read_text())
    data["artifacts"][0]["foothold"]["name"] = "managed-ot"
    path = tmp_path / "chains.json"
    path.write_text(json.dumps(data))
    chain = chains.load(path)[0]
    assert chain.exclusion() == "its foothold managed-ot starts off the adversary host"


def test_a_declared_condition_that_the_table_holds_keeps_its_chain_in() -> None:
    """The chain that needs a running audit declares the condition that stands
    for it, so only the need itself keeps the chain out."""
    harvest = by_id("it-foothold__exfiltrate-replica-telemetry-1")
    assert harvest.needs == {chains.AUDIT_RUNNING}
    assert harvest.unmatched == ()


@pytest.mark.parametrize(
    "condition",
    ["operator-hmi-activity-during-siem-inventory", "a-condition-added-later"],
)
def test_a_declared_condition_without_its_need_keeps_its_chain_out(
    tmp_path: Path, condition: str
) -> None:
    """A condition for which the table holds no need of the chain's stages,
    known or not, keeps the chain out: the review and the stages disagree."""
    data = json.loads(SAMPLE.read_text())
    data["artifacts"][0]["conditions"] = [
        {
            "id": condition,
            "timing": "during-stage",
            "stage": "enumerate-siem-agent-inventory",
            "description": "",
        }
    ]
    path = tmp_path / "chains.json"
    path.write_text(json.dumps(data))
    chain = chains.find(chains.load(path), "it-foothold__disclose-account-inventory")
    assert chain.unmatched == (condition,)
    assert chain.exclusion() == (
        f"the table holds no need of its stages for its conditions {condition}"
    )


def test_a_support_artifact_holds_no_chain(tmp_path: Path) -> None:
    data = json.loads(SAMPLE.read_text())
    data["artifacts"].append(
        {
            "kind": "support",
            "binary": "mock-scan-test__scan-contract",
            "foothold": "it-foothold",
            "purpose": "",
        }
    )
    path = tmp_path / "chains.json"
    path.write_text(json.dumps(data))
    assert [c.id for c in chains.load(path)] == [c.id for c in sample()]


def test_a_mock_builds_catalogue_is_refused(tmp_path: Path) -> None:
    """A mock build's binaries take fixed facts in place of the scan of the IT
    segment, so they would not act as the attack chains do."""
    data = json.loads(SAMPLE.read_text())
    data["generator"]["mock_build"] = True
    path = tmp_path / "chains.json"
    path.write_text(json.dumps(data))
    with pytest.raises(chains.CatalogueError, match="mock build"):
        chains.load(path)


def test_a_catalogue_of_another_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "chains.json"
    path.write_text(json.dumps({"format": 1, "chains": []}))
    with pytest.raises(chains.CatalogueError, match="schema_version None"):
        chains.load(path)
    path.write_text(json.dumps({"schema_version": 3, "artifacts": []}))
    with pytest.raises(chains.CatalogueError, match="schema_version 3"):
        chains.load(path)
    with pytest.raises(chains.CatalogueError, match="cannot read"):
        chains.load(tmp_path / "absent.json")


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda a: a.pop("impact_stage"), "no catalogue with schema_version 2"),
        (lambda a: a.update(kind="test"), "kind 'test'"),
        (lambda a: a["stages"].reverse(), "positions"),
    ],
)
def test_a_malformed_artifact_is_refused(tmp_path: Path, change, match: str) -> None:
    data = json.loads(SAMPLE.read_text())
    change(data["artifacts"][0])
    path = tmp_path / "chains.json"
    path.write_text(json.dumps(data))
    with pytest.raises(chains.CatalogueError, match=match):
        chains.load(path)


def test_a_chain_is_drawn_from_the_library() -> None:
    library = chains.library(sample())
    drawn = {chains.choose(library, random.Random(n)).id for n in range(50)}
    assert drawn == {c.id for c in library}
    with pytest.raises(chains.CatalogueError, match="no chain"):
        chains.choose([])


def test_the_binaries_are_found_by_the_chain_names(tmp_path: Path) -> None:
    disclose = by_id("it-foothold__disclose-account-inventory")
    assert chains.unbuilt([disclose], tmp_path) == [disclose.id]
    (tmp_path / disclose.id).write_bytes(b"\x7fELF binary")
    assert chains.unbuilt([disclose], tmp_path) == []
    assert disclose.sha256(tmp_path) == hashlib.sha256(b"\x7fELF binary").hexdigest()
    with pytest.raises(chains.CatalogueError, match="no chain"):
        chains.find(sample(), "absent")


@pytest.mark.skipif(
    not chains.CATALOGUE.is_file(), reason="no chain catalogue: run make build-attacks"
)
def test_every_catalogued_stage_is_reviewed_and_takes_a_target() -> None:
    """The reviewed table covers the whole library of stages, and names no
    stage that the library has dropped."""
    catalogued = chains.load()
    names = {s.name for c in catalogued for s in c.stages}
    assert not [c.id for c in catalogued if c.unreviewed]
    assert set(chains.REVIEWED) == names


def reviewed_source(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """The attack chains' crates, taken as reviewed, and a catalogue built
    after them: the main crate's source, manifest and lock file, the runtime
    crate's source and manifest, and a test, an example and build output,
    which the review leaves out."""
    src = tmp_path / "attack-chains"
    files = {
        "Cargo.toml": '[package]\nname = "attack-chains"\n',
        "Cargo.lock": "version = 4\n",
        "src/library.rs": "fn stage() {}\n",
        "src/rdp/session.rs": "fn open() {}\n",
        "runtime/Cargo.toml": '[package]\nname = "runtime"\n',
        "runtime/src/lib.rs": '#[path = "../../src/library.rs"]\nmod library;\n',
        "tests/hardening.rs": "#[test]\nfn stripped() {}\n",
        "examples/demo.rs": "fn main() {}\n",
        "target/release/build.rs": "fn main() {}\n",
    }
    for name, text in files.items():
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        (src / name).write_text(text)
    monkeypatch.setattr(chains, "REVIEWED_SOURCE_SHA256", chains.source_digest(src))
    catalogue = tmp_path / "chains.json"
    catalogue.write_text("{}")
    os.utime(catalogue, (time.time() + 10, time.time() + 10))
    return src, catalogue


def test_the_reviewed_source_passes_its_check(tmp_path: Path, monkeypatch) -> None:
    src, catalogue = reviewed_source(tmp_path, monkeypatch)
    chains.check_review(src, catalogue)


def test_any_change_to_the_source_asks_for_a_new_review(
    tmp_path: Path, monkeypatch
) -> None:
    src, catalogue = reviewed_source(tmp_path, monkeypatch)
    (src / "src" / "rdp" / "session.rs").write_text("fn open() { restart() }\n")
    os.utime(catalogue, (time.time() + 20, time.time() + 20))
    with pytest.raises(chains.CatalogueError, match="review REVIEWED") as raised:
        chains.check_review(src, catalogue)
    assert chains.source_digest(src) in str(raised.value)
    # A file moved to another path is a change too.
    src, catalogue = reviewed_source(tmp_path / "moved", monkeypatch)
    (src / "src" / "rdp" / "session.rs").rename(src / "src" / "session.rs")
    with pytest.raises(chains.CatalogueError, match="review REVIEWED"):
        chains.check_review(src, catalogue)


@pytest.mark.parametrize(
    "name",
    ["Cargo.toml", "Cargo.lock", "runtime/Cargo.toml", "runtime/src/lib.rs"],
)
def test_a_change_to_a_manifest_lock_file_or_the_runtime_crate_asks_for_a_new_review(
    tmp_path: Path, monkeypatch, name: str
) -> None:
    """The release profile, the dependencies and the runtime crate decide
    what the binaries do (episodes.md §10.1, the attack chains)."""
    src, catalogue = reviewed_source(tmp_path, monkeypatch)
    with (src / name).open("a") as changed:
        changed.write("# changed\n")
    os.utime(catalogue, (time.time() + 20, time.time() + 20))
    with pytest.raises(chains.CatalogueError, match="review REVIEWED"):
        chains.check_review(src, catalogue)


@pytest.mark.parametrize(
    "name", ["tests/hardening.rs", "examples/demo.rs", "target/release/build.rs"]
)
def test_a_change_to_a_test_an_example_or_build_output_needs_no_review(
    tmp_path: Path, monkeypatch, name: str
) -> None:
    src, catalogue = reviewed_source(tmp_path, monkeypatch)
    with (src / name).open("a") as changed:
        changed.write("// changed\n")
    os.utime(src / name, (time.time() + 30, time.time() + 30))
    chains.check_review(src, catalogue)


# The catalogue's path in a release's dist, as the episode runner reads it:
# chains.json beside the binaries, each named by its chain's id.
CATALOGUE_IN_DIST = chains.CATALOGUE.relative_to(chains.DIST).as_posix()


def release_binaries(tmp_path: Path, monkeypatch) -> Path:
    """A release's dist without the source: two binaries and the catalogue,
    with the list of their digests and the review digest of their source."""
    monkeypatch.setattr(chains, "REVIEWED_SOURCE_SHA256", "ab" * 32)
    dist = tmp_path / "dist"
    files = {
        "it-foothold__deny": b"\x7fELF binary one",
        "it-foothold__exfiltrate": b"\x7fELF binary two",
        CATALOGUE_IN_DIST: b"{}",
    }
    for name, data in files.items():
        (dist / name).parent.mkdir(parents=True, exist_ok=True)
        (dist / name).write_bytes(data)
    (dist / chains.DIST_DIGESTS).write_text(
        "".join(
            f"{hashlib.sha256(data).hexdigest()}  {name}\n"
            for name, data in files.items()
        )
    )
    (dist / chains.DIST_SOURCE).write_text("ab" * 32 + "\n")
    return dist


def test_a_release_without_the_source_checks_its_prebuilt_binaries(
    tmp_path: Path, monkeypatch
) -> None:
    """episodes.md §10.1: without the source, binaries that match their list
    and were built from the reviewed source run."""
    dist = release_binaries(tmp_path, monkeypatch)
    chains.check_review(tmp_path / "no-source", dist / CATALOGUE_IN_DIST, dist)


@pytest.mark.parametrize(
    "change, refusal",
    [
        (lambda d: (d / "it-foothold__deny").write_bytes(b"changed"), "differs from"),
        (lambda d: (d / "it-foothold__deny").unlink(), "is missing"),
        (lambda d: (d / "extra").write_bytes(b"x"), "is not in"),
        (
            lambda d: (d / chains.DIST_SOURCE).write_text("cd" * 32),
            "built from the source",
        ),
        (lambda d: (d / chains.DIST_DIGESTS).unlink(), "no attack chains' source"),
    ],
)
def test_a_release_whose_binaries_differ_is_refused(
    tmp_path: Path, monkeypatch, change, refusal: str
) -> None:
    dist = release_binaries(tmp_path, monkeypatch)
    change(dist)
    with pytest.raises(chains.CatalogueError, match=refusal):
        chains.check_review(tmp_path / "no-source", dist / CATALOGUE_IN_DIST, dist)


def test_binaries_older_than_a_manifest_are_refused(
    tmp_path: Path, monkeypatch
) -> None:
    src, catalogue = reviewed_source(tmp_path, monkeypatch)
    os.utime(src / "Cargo.toml", (time.time() + 30, time.time() + 30))
    with pytest.raises(chains.CatalogueError, match="make build-attacks"):
        chains.check_review(src, catalogue)


def test_a_binary_older_than_its_source_is_refused(tmp_path: Path, monkeypatch) -> None:
    src, catalogue = reviewed_source(tmp_path, monkeypatch)
    os.utime(catalogue, (time.time() - 3600, time.time() - 3600))
    with pytest.raises(chains.CatalogueError, match="make build-attacks"):
        chains.check_review(src, catalogue)
    # Without the source, and with no release's lists in dist, nothing runs.
    with pytest.raises(chains.CatalogueError, match="no attack chains' source"):
        chains.check_review(tmp_path / "absent", catalogue, tmp_path / "no-release")


@pytest.mark.skipif(
    not chains.SOURCE.is_dir() or not chains.source_files(),
    reason="no attack chains' source in this tree",
)
def test_a_change_to_the_attack_chains_source_is_reported() -> None:
    """A change to the attack chains' source raises a warning here, while the
    episode runner refuses adversarial episodes until REVIEWED is reviewed
    against the change and REVIEWED_SOURCE_SHA256 updated."""
    digest = chains.source_digest()
    if digest != chains.REVIEWED_SOURCE_SHA256:
        warnings.warn(
            "the attack chains' source has changed since its stages were reviewed "
            f"({chains.REVIEWED_SOURCE}): review REVIEWED in chains.py against the "
            f"change, then set REVIEWED_SOURCE_SHA256 to {digest}",
            stacklevel=1,
        )

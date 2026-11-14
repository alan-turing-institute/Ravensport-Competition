"""
Self-monitoring scrape tests for `hist-01` (hist.md §7.1, §9.1)

§7.1 gives `hist-01` an operations dashboard showing ingestion rate, remote-write
queue depth and store health. Every figure behind those panels is exposed on one
of the four local `/metrics` endpoints and, until this scrape configuration
existed, was stored nowhere: `GET /api/v1/label/__name__/values` against the
deployed `hist-01` returned only the two series the FMS pushes. All three panels
would have rendered empty behind a green build and a green unit suite.

Two properties are asserted here, and the second is the reason the scraping sits
where it does.

**Everything §7.1 names is collected.** Four endpoints, not one: the stores expose
their own uptime and disk figures, but ingestion and replication live on the
agents, so a configuration reaching only VictoriaMetrics would leave two of the
three panels unbuildable.

**The scraped series stay on `hist-01`.** They are the operational-technology
host's own internal telemetry, and §6.1 holds that they do not cross into the
demilitarised zone — the one exception to §6's replication. The scrape runs on
`victoria-metrics`, which stores locally and has no remote-write of any kind,
rather than on `vmagent`, whose every sample goes to both of §6's targets. Placed
on the agent, the containment would depend on a `-remoteWrite.urlRelabelConfig`
positional array whose entries map to `-remoteWrite.url` in order — and a mistake
there fails in the direction of sending the telemetry to the replica, silently.
Here the containment is structural, and the test below states it as such.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import functools
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]  # deploy/
sys.path.insert(0, str(ROOT / "scripts"))
import render  # noqa: E402

# The four local endpoints of §9.1's flag table, and the component behind each.
# Every one is on loopback: `hist-01`'s stores bind loopback only, and the agents
# are scraped over it too rather than over the zone address they also carry.
_EXPECTED_TARGETS = {
    "victoria-metrics": "127.0.0.1:8428",
    "vmagent": "127.0.0.1:8429",
    "victoria-logs": "127.0.0.1:9428",
    "vlagent": "127.0.0.1:9429",
}

# `hist-dmz-01` runs neither agent (§10.3) and carries no operations dashboard
# (§7.1), so it scrapes nothing.
_SCRAPING_NODES = ["hist-01"]
_NON_SCRAPING_NODES = ["hist-dmz-01"]


@functools.lru_cache(maxsize=None)
def _render(profile: str = "ci-minimal") -> Path:
    out = subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return ROOT / "build"


def _script(node: str) -> str:
    return (_render() / "node-build" / f"{node}.sh").read_text()


def _scrape_config_path(node: str) -> Path:
    return _render() / "victoria-metrics" / f"{node}-scrape.yml"


def _scrape_config(node: str) -> dict:
    path = _scrape_config_path(node)
    assert path.exists(), (
        f"{path.relative_to(ROOT)} was not rendered; hist.md §7.1's operations "
        f"dashboard has no data without it"
    )
    return yaml.safe_load(path.read_text())


def _exec_start(node: str, service: str) -> str:
    """The ExecStart command line the build script writes for `service`, joined
    onto one line so that a flag is found regardless of where the unit wraps."""
    script = _script(node)
    unit = re.search(
        rf"/etc/systemd/system/{re.escape(service)}\.service.*?<<\s*'?([A-Za-z_]+)'?\s*\n"
        rf"(.*?)\n\1\s*\n",
        script,
        re.DOTALL,
    )
    assert unit, f"{node}: no {service}.service unit found in the rendered script"
    body = unit.group(2)
    command = re.search(r"^ExecStart=(.*?)(?=^\w+=|\Z)", body, re.DOTALL | re.MULTILINE)
    assert command, f"{node}: {service}.service has no ExecStart"
    return " ".join(command.group(1).replace("\\\n", " ").split())


# ── The scrape configuration ──────────────────────────────────────────────────


@pytest.mark.parametrize("node", _SCRAPING_NODES)
def test_every_local_endpoint_is_scraped(node: str):
    """§7.1 names three classes of figure and they live on different processes:
    uptime and disk usage on the two stores, ingestion and remote-write queue
    depth on the two agents. A configuration reaching a subset renders the
    corresponding panel empty rather than failing."""
    config = _scrape_config(node)
    scraped = {
        target
        for job in config["scrape_configs"]
        for static in job["static_configs"]
        for target in static["targets"]
    }
    missing = set(_EXPECTED_TARGETS.values()) - scraped
    assert not missing, (
        f"{node}: {sorted(missing)} are not scraped, so the §7.1 panels drawing "
        f"on them would render empty. Scraped: {sorted(scraped)}"
    )


@pytest.mark.parametrize("node", _SCRAPING_NODES)
def test_each_component_is_its_own_job(node: str):
    """The job name becomes the `job` label on every scraped series, and §7.1's
    "ingestion rate per source" is read from it. One job holding all four targets
    would collect the same samples and leave no label to separate them by."""
    config = _scrape_config(node)
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}

    assert set(jobs) == set(_EXPECTED_TARGETS), (
        f"{node}: job names are {sorted(jobs)}; §9.1's four components are "
        f"{sorted(_EXPECTED_TARGETS)}"
    )
    for name, expected in _EXPECTED_TARGETS.items():
        targets = [t for s in jobs[name]["static_configs"] for t in s["targets"]]
        assert targets == [expected], (
            f"{node}: job {name!r} scrapes {targets}, not [{expected!r}] — the "
            f"`job` label would then name the wrong component"
        )


@pytest.mark.parametrize("node", _SCRAPING_NODES)
def test_scraping_stays_on_loopback(node: str):
    """`hist-01`'s stores bind loopback only (§9.2's asymmetry), and the agents
    are reached the same way even though they also bind the zone address. A
    scrape addressed to the zone address would leave the host's own telemetry
    dependent on its external interface being up — the interface whose failure
    the dashboard exists to show."""
    for job in _scrape_config(node)["scrape_configs"]:
        for static in job["static_configs"]:
            for target in static["targets"]:
                assert target.startswith("127.0.0.1:"), (
                    f"{node}: job {job['job_name']!r} scrapes {target!r}, which is "
                    f"not loopback"
                )


# ── Containment: the scraped telemetry does not leave hist-01 ─────────────────


@pytest.mark.parametrize("node", _SCRAPING_NODES)
def test_the_scraper_has_no_remote_write(node: str):
    """The property that keeps `hist-01`'s internal telemetry out of the
    demilitarised zone, stated as the structural fact it is.

    Whichever process carries `-promscrape.config` sends what it scrapes wherever
    its own remote-write points. On `victoria-metrics` there is no remote-write to
    point anywhere, so the samples can only land in the local store. Moving the
    flag to `vmagent` — whose two `-remoteWrite.url` targets are §6's, one of them
    `hist-dmz-01` — would replicate them, which §6.1 rules out. This asserts the
    placement rather than the file, because it is the placement that provides the
    guarantee.
    """
    scrapers = [
        service
        for service in _EXPECTED_TARGETS
        if "-promscrape.config" in _exec_start(node, service)
    ]
    assert scrapers, (
        f"{node}: no service carries -promscrape.config, so nothing collects the "
        f"metrics §7.1's operations dashboard displays"
    )
    for service in scrapers:
        assert "-remoteWrite.url" not in _exec_start(node, service), (
            f"{node}: {service} both scrapes and remote-writes, so everything it "
            f"scrapes is forwarded to §6's targets — including hist-dmz-01. The "
            f"host's own telemetry is not to cross the zone boundary"
        )


@pytest.mark.parametrize("node", _SCRAPING_NODES)
def test_the_scraper_reads_the_file_the_build_pushes(node: str):
    """A `-promscrape.config` naming a path the build does not fill leaves
    VictoriaMetrics logging a missing file at start and scraping nothing, while
    the unit stays active and the dashboard stays empty."""
    exec_start = _exec_start(node, "victoria-metrics")
    assert f"-promscrape.config={render.SCRAPE_CONFIG_PATH}" in exec_start, (
        f"{node}: victoria-metrics.service does not read "
        f"{render.SCRAPE_CONFIG_PATH}; ExecStart is {exec_start!r}"
    )
    assert f'"$BUILD_CTR{render.SCRAPE_CONFIG_PATH}"' in _script(node), (
        f"{node}: the build script does not push a file to "
        f"{render.SCRAPE_CONFIG_PATH}, which the unit above reads"
    )


@pytest.mark.parametrize("node", _SCRAPING_NODES)
def test_the_scrape_config_is_readable_by_the_service_account(node: str):
    """`victoria-metrics` runs unprivileged (§10.2). A config file it cannot read
    is the same failure as one that is absent, and neither stops the unit."""
    script = _script(node)
    # The push wraps onto a continuation line, so the match spans one.
    pushed = re.search(
        rf"incus file push(?:[^\n]|\\\n)*{re.escape(render.SCRAPE_CONFIG_PATH)}\"",
        script,
    )
    assert pushed, f"{node}: no push of {render.SCRAPE_CONFIG_PATH} found"
    directory = str(PurePosixPath(render.SCRAPE_CONFIG_PATH).parent)
    assert re.search(
        rf"chown(?:[^\n]|\\\n)*victoria-metrics(?:[^\n]|\\\n)*{re.escape(directory)}",
        script,
    ), (
        f"{node}: {render.SCRAPE_CONFIG_PATH} is never chowned to the "
        f"victoria-metrics account that must read it"
    )


@pytest.mark.parametrize("node", _NON_SCRAPING_NODES)
def test_the_replica_scrapes_nothing(node: str):
    """`hist-dmz-01` runs neither agent (§10.3) and §7.1 gives it the bunkering
    dashboard, not an operations one. Scraping there would put the replica's
    internal telemetry into the store the reconciliation user audits, among the
    records that audit is about."""
    assert not _scrape_config_path(node).exists(), (
        f"{node}: a scrape configuration was rendered for the replica"
    )
    for service in ("victoria-metrics", "victoria-logs"):
        assert "-promscrape.config" not in _exec_start(node, service), (
            f"{node}: {service} scrapes, which §7.1 does not ask the replica to do"
        )


# ── The input hash ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("node", _SCRAPING_NODES)
def test_the_scrape_config_joins_the_input_hash(node: str):
    """`deployment.md §9`: a change to a file baked into the image must rebuild
    it. Editing the scrape configuration changes what the deployed host collects,
    so an image left standing on the old hash would keep collecting the old set
    while the repository said otherwise."""
    script = _script(node)
    hash_block = re.search(
        r"current_hash=\$\(sha256sum(.*?)\|\s*sha256sum", script, re.DOTALL
    )
    assert hash_block, f"{node}: no current_hash block found"
    assert f"{node}-scrape.yml" in hash_block.group(1), (
        f"{node}: build/victoria-metrics/{node}-scrape.yml is not among the input "
        f"hash's files, so editing it would not rebuild the image"
    )

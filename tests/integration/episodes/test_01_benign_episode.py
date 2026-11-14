"""One benign suppressed episode of the first version (episodes-tests.md §0.6,
the first version).

Run with: make -C tests/integration test-episodes
"""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import render

from competition.episode_runner import chains, deploy, predicted_events
from competition.episode_runner.record import FILES
from competition.episode_runner.startup import FIELDS, start_state, startup_input
from tests.integration.episodes.conftest import (
    ADVERSARY,
    REPO,
    TRACK,
    EpisodeRun,
    exported,
    image_store,
    ossec_time,
    parse,
)

HORIZON = 144
STEP = timedelta(seconds=5)
# The settle delay of episodes.md §6.6 (settle period).
SETTLE_DELAY = timedelta(seconds=45)
# The agent's check-in interval is Wazuh's default notify_time, because the
# agents' configuration leaves it unset. An agent's log gives times to the
# second.
CHECK_IN = timedelta(seconds=10)
LOG_RESOLUTION = timedelta(seconds=1)
COMPETITION_IMAGE = "ravensport/siem-01-competition:latest"
# The firewall nodes, each of which runs from its competition firewall image in
# a competition episode (episodes.md §3.4, competition image).
FIREWALLS = ("fw-ext-dmz", "fw-dmz-it", "fw-ot-dmz")
FLOW_SOURCES = ("/var/log/ulog/connections.json", "/var/log/suricata/eve.json")


def step_window(run: EpisodeRun) -> tuple:
    step_0 = parse(run.record["settle"]["step_0"])
    return step_0, step_0 + HORIZON * STEP


def foreign_flows(run: EpisodeRun, start, end) -> tuple[int, list[str]]:
    """The number of flow records that the manager received from start to
    end, and every address in them outside the zone networks. IPv6
    link-local and multicast addresses belong to the bridges' neighbour
    discovery, and the unspecified address `::` to its duplicate address
    detection, whose neighbour solicitations an interface sends before it holds
    an address."""
    zones = [
        ipaddress.ip_network(net["cidr"]) for net in run.topology["networks"].values()
    ]
    count, foreign = 0, []
    for line in exported(run.path, "archives").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("location") not in FLOW_SOURCES:
            continue
        if not start <= parse(event["timestamp"]) <= end:
            continue
        count += 1
        for key in ("src_ip", "dest_ip"):
            if key not in event.get("data", {}):
                continue
            address = ipaddress.ip_address(event["data"][key])
            if not (
                any(address in zone for zone in zones)
                or address.is_link_local
                or address.is_multicast
                or address.is_unspecified
            ):
                foreign.append(f"{event['location']}: {address}")
    return count, foreign


# §3, start state and start point


def test_a_file_integrity_scan_is_requested_at_the_drawn_time(episode_run) -> None:
    record = episode_run.record
    step_0, end = step_window(episode_run)
    inside = sorted(
        (parse(at), host)
        for host, at in record["first_scans"].items()
        if step_0 <= parse(at) < end
    )
    assert inside, "the suite's seed draws no first scan inside the episode"
    requests = record["scan_requests"]
    assert [r["host"] for r in requests] == [host for _, host in inside]
    for request, (due, _) in zip(requests, inside, strict=True):
        sent = parse(request["sent"])
        assert request["ok"], request["output"]
        assert timedelta(0) <= sent - due < timedelta(seconds=1)
        started = [
            ossec_time(line)
            for line in episode_run.before_reset["scans_started"][request["host"]]
        ]
        assert any(sent - LOG_RESOLUTION <= t <= sent + CHECK_IN for t in started), (
            f"{request['host']} started no scan within {CHECK_IN} of the request"
        )


def test_adversary_host_stays_silent_in_a_benign_episode(episode_run) -> None:
    """The adversary host's interfaces are down and unaddressed at step 0 and
    after the export, and no record that the manager received names an address
    of the adversary host."""
    if ADVERSARY not in episode_run.record["images"]:
        pytest.skip("the topology defines no adversary host")
    for moment in (episode_run.at_step_0, episode_run.before_reset):
        interfaces = moment["adversary_addresses"]
        assert interfaces, "the adversary host lists no interface"
        assert all(not i["up"] and not i["ipv4"] for i in interfaces.values())
    archives = exported(episode_run.path, "archives")
    for nic in episode_run.topology["nodes"][ADVERSARY]["nics"]:
        address = re.escape(nic["ipv4"])
        assert not re.search(rf"(?<![\d.]){address}(?![\d])", archives), nic["ipv4"]


# §1, episode inputs and seeds


def test_start_up_input_is_attached_before_sim_host_starts(episode_run) -> None:
    at = episode_run.at_step_0
    device = at["instances"]["sim-host"]["devices"]["startup-input"]
    assert device["type"] == "disk"
    assert device["path"] == "/var/lib/sim/startup"
    assert device["readonly"] == "true"
    startup = (episode_run.path / "startup.json").read_text()
    assert at["startup_as_sim_user"] == startup
    assert at["state_dir"] == ["startup.json"]
    # The competition's entry point logged the seed and the start state of the
    # start-up input at its start.
    fields = json.loads(startup)
    line = episode_run.record["settle"]["start_state"]
    assert f"sim_tug_arrivals={fields['sim_tug_arrivals']} " in line
    assert 'delivery_state="no order placed"' in line
    # No other file reaches a node: no node has a disk other than its root.
    for name, instance in at["instances"].items():
        disks = sorted(
            device_name
            for device_name, config in instance["expanded_devices"].items()
            if config["type"] == "disk" and config.get("path") != "/"
        )
        assert disks == (["startup-input"] if name == "sim-host" else []), name


# §4, settle period


def test_step_0_waits_the_settle_delay(episode_run) -> None:
    settle = episode_run.record["settle"]
    held = [parse(c["held"]) for c in settle["conditions"]]
    assert [c["condition"] for c in settle["conditions"]] == [1, 2, 3, 4]
    assert all(c["lines"] for c in settle["conditions"])
    step_0 = parse(settle["step_0"])
    assert step_0 - max(held) >= SETTLE_DELAY - timedelta(milliseconds=1)
    assert parse(episode_run.steps[0]["request_sent"]) >= step_0


def test_settle_checks_send_no_network_traffic(episode_run) -> None:
    start = parse(episode_run.record["settle"]["start"])
    step_0, _ = step_window(episode_run)
    count, foreign = foreign_flows(episode_run, start, step_0)
    assert count, "the manager received no flow record during the settle period"
    assert not foreign


# §5, step protocol


def test_step_messages_send_no_network_traffic(episode_run) -> None:
    count, foreign = foreign_flows(episode_run, *step_window(episode_run))
    assert count, "the manager received no flow record during the steps"
    assert not foreign


def test_the_idle_defender_gives_a_decision_for_every_step(episode_run) -> None:
    lines = episode_run.steps
    assert [line["step"] for line in lines] == list(range(HORIZON))
    assert all(line["decision"] == "no action" for line in lines)
    assert all(line["decision_arrival"] is not None for line in lines)
    assert all(line["dropped_decision_messages"] == 0 for line in lines)
    assert all(line["invalid_lines"] == 0 for line in lines)
    assert episode_run.record["steps"]["defender_exit_status"] == 0


# §9, horizon and termination


def test_an_episode_ends_at_the_horizon(episode_run) -> None:
    assert (episode_run.outcome.outcome, episode_run.outcome.cause) == ("horizon", None)
    steps = episode_run.record["steps"]
    assert steps["run"] == HORIZON
    assert steps["ended_early"] is None
    _, end = step_window(episode_run)
    assert parse(steps["end_notice_sent"]) >= end


# §11, episode record


def test_the_episode_record_holds_every_item(episode_run) -> None:
    """The items of the first version and the record of reads. The attack
    chain and the stage events belong to adversarial episodes, and the ground
    truth and the verdicts to later versions."""
    path = episode_run.path
    assert sorted(p.name for p in path.iterdir()) == sorted(FILES)
    record = episode_run.record
    for key in (
        "episode_seed",
        "type",
        "mode",
        "scenario",
        "images",
        "settle",
        "first_scans",
        "scan_requests",
        "steps",
        "export",
        "reset",
        "durations_s",
    ):
        assert key in record, key
    assert (record["episode_seed"], record["type"], record["mode"]) == (
        episode_run.seed,
        "benign",
        "evaluation",
    )
    assert record["track"] == TRACK
    # The record's name holds the episode's plan to the second, from which the
    # start-up input places the next audit.
    planned = datetime.strptime(path.name.split("-")[0], "%Y%m%dT%H%M%SZ")
    assert (path / "startup.json").read_text() == startup_input(
        episode_run.seed,
        predicted_events.competition_timings(),
        planned.replace(tzinfo=timezone.utc),
    )
    assert all(error is None for error in record["export"].values())
    for output in ("alerts", "archives"):
        lines = exported(path, output).splitlines()
        assert lines, output
        json.loads(lines[0])
    assert record["settle"]["start_state"] in (path / "sim.log").read_text()
    assert set(record["durations_s"]) == {
        "plan",
        "deploy",
        "settle",
        "steps",
        "end",
        "export",
        "reset",
    }


def test_no_node_can_reach_the_episode_record(episode_run) -> None:
    at = episode_run.at_step_0
    # The host holds no address on any zone network.
    for name, network in at["networks"].items():
        assert network["config"].get("ipv4.address") == "none", name
        assert network["config"].get("ipv6.address") == "none", name
    records = Path(episode_run.records).resolve()
    for name, instance in at["instances"].items():
        for device_name, config in instance["expanded_devices"].items():
            if config["type"] == "nic":
                parent = config.get("network") or config.get("parent")
                assert parent in at["networks"], f"{name} {device_name}: {parent}"
            if config["type"] == "disk" and "source" in config:
                assert not Path(config["source"]).resolve().is_relative_to(records)


def test_image_hashes_match_the_deployed_images(episode_run) -> None:
    store = image_store()
    fingerprints = set(store.values())
    for name, image in episode_run.record["images"].items():
        deployed = episode_run.at_step_0["instances"][name]["config"]
        assert image["fingerprint"] == deployed["volatile.base_image"], name
        assert image["fingerprint"] in fingerprints, name
        if image["image"]:
            assert store[image["image"]] == image["fingerprint"], name


def test_the_episode_type_appears_only_in_the_episode_record(episode_run) -> None:
    """The episode runner places the start-up input on sim-host, and the
    adversary host's own image holds every attack chain's binary in every
    episode, benign or adversarial (episodes.md §6.5, adversary host).
    Neither states the episode type."""
    startup = json.loads(episode_run.at_step_0["startup_as_sim_user"])
    # The seed of the tug stream and the simulator's own start state, which
    # every episode holds, benign or adversarial.
    assert tuple(startup) == FIELDS
    assert isinstance(startup.pop("sim_tug_arrivals"), int)
    startup.pop("next_audit_at")
    assert startup == start_state(predicted_events.competition_timings())
    assert episode_run.at_step_0["state_dir"] == ["startup.json"]
    if ADVERSARY in episode_run.record["images"]:
        held = episode_run.at_step_0["adversary_binaries"]
        for chain in chains.load():
            assert held.get(chain.id) == chain.sha256(), chain.id


# §12, reset and the competition image


def test_a_living_port_creates_its_nodes_from_the_competition_images(
    episode_run,
) -> None:
    """Each node that the competition's additional override file gives an
    image runs from that competition image: siem-01, sim-host and each
    firewall node. Every other node, the adversary host among them where the
    deployment holds one, runs from the image that render.py names for it in
    the deployed topology: the image that the profile's deployment names
    without the file. The deployment's unit tests check that the topology's
    nodes then keep their operational images."""
    images = episode_run.record["images"]
    store = image_store()
    override = render.load_yaml(REPO / "competition" / "deploy" / "override.yaml")
    # A node built by a build script takes an image name, and sim-host, which
    # build-sim-image.sh builds, takes an image.
    competition = {
        name: node.get("image_name", node.get("image"))
        for name, node in override["nodes"].items()
        if "image_name" in node or "image" in node
    }
    assert competition["siem-01"] == COMPETITION_IMAGE
    assert competition["sim-host"] == "ravensport/sim-competition:latest"
    assert {f"ravensport/{name}-competition:latest" for name in FIREWALLS} <= set(
        competition.values()
    )
    for name, image in competition.items():
        assert images[name]["image"] == image, name
        assert images[name]["fingerprint"] == store[image], name
    nodes = deploy.topology(episode_run.record["profile"])["nodes"]
    for name, image in images.items():
        if name not in competition:
            assert image["image"] == render.node_image(name, nodes[name]), name


def test_reset_returns_every_host_to_its_state_at_deployment(episode_run) -> None:
    """The reset deletes every RavensPort node, so the next deployment creates
    each node afresh from its image. The test checks the deletion. The checks
    of the next episode's start need a second episode, which the suite does
    not run."""
    assert episode_run.after_reset["remaining"] == []
    assert episode_run.record["reset"]["remaining"] == []

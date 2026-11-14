"""
The snapshot-set scripts (deployment.md §8.1, §8.2, §10): taking a set,
restoring it as a run, stopping the restored run, and moving a set between hosts
as a snapshot file.

Each script runs in a copy of deploy/ against stub commands: `incus`
(stub_incus.py), which keeps the instances, snapshots, devices and networks in
a state file and models the sim service on sim-host; `sudo`, `systemctl` and
`sleep`, which record their arguments and do nothing else, so the host clock is
never changed; `git`, for the commit an import compares; and `make`, which
records its arguments so that a test can show nothing is rendered. The copy's
build/ holds rendered files and render inputs as `make render` leaves them, and
the sets are kept in its snapshots/.
Every stub appends to one log, so the tests assert the order of the steps as
well as the end state:

  * a set is taken by stopping the sim first, so that it saves its checkpoint,
    then the rest in the order of a planned maintenance window, and a sim that
    did not come to rest leaves no set;
  * a failure once the sim's checkpoint is confirmed keeps the set, marked
    incomplete, and taking the set again completes it, unless the
    environment has run since or no restore could start the sim;
  * a set whose fuel supplier poll falls due at the start of a restored run is
    taken with a warning;
  * a restore holds the host clock at the set's stop time before any instance
    starts, starts the instances in the reverse of the stop order, and starts
    the sim last, from a read-only copy of the checkpoint, only while the
    checkpoint is young enough for the sim to accept, and fails if the sim
    stops within 30 seconds;
  * stopping a restored run stops every instance before the clock is released,
    and `make stop` changes nothing when no restored run is in progress;
  * `make down` changes nothing while a restored run is in progress or an instance
    holds a set, unless forced, and `make deploy` refuses while the clock is
    held;
  * a set carries the rendered files its instances came from, and a restore,
    the stop of its run, a later set and `make down` use those files, never
    the local render;
  * a set exported on one host restores on another that has no render of its
    own.

What these stubs cannot show (the size of a real export, the read-only mount
in an unprivileged container, the sim's own acceptance of the checkpoint) is
covered by tests/integration/test_snapshot.sh against a live deployment.

Run with: pytest tests/unit -q
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # deploy/
SIM_SRC = ROOT.parent / "infra" / "sim" / "src"
STUB_INCUS = Path(__file__).with_name("stub_incus.py")
SCRIPTS = [
    "snapshot-lib.sh",
    "snapshot.sh",
    "restore.sh",
    "stop.sh",
    "export-snapshot.sh",
    "import-snapshot.sh",
    "teardown.sh",
    "deploy.sh",
]

NODES = ["fw-ot-dmz", "hist-01", "fms-01", "hmi-01", "sim-host"]
STOP_ORDER = ["hmi-01", "fms-01", "hist-01", "fw-ot-dmz"]
NETWORKS = ["vnet-ext", "vnet-dmz", "vnet-it", "vnet-ot"]
HEAD = "a" * 40
DROPIN = "/etc/systemd/system/lima-guestagent.service.d/no-set-time.conf"

# The commands a stub records and otherwise ignores.
RECORDING_STUBS = {
    "sudo": """#!/bin/sh
echo "sudo $*" >>"$STUB_LOG"
if [ "$1" = tee ]; then
    while IFS= read -r line; do echo "tee> $line" >>"$STUB_LOG"; done
fi
""",
    "systemctl": """#!/bin/sh
echo "systemctl $*" >>"$STUB_LOG"
if [ "$1 $2" = "cat lima-guestagent.service" ]; then [ "$STUB_LIMA" = 1 ]; fi
""",
    "sleep": """#!/bin/sh
echo "sleep $*" >>"$STUB_LOG"
""",
    "git": """#!/bin/sh
[ "$1 $2" = "rev-parse HEAD" ] && echo "$STUB_GIT_HEAD"
""",
    "make": """#!/bin/sh
echo "make $*" >>"$STUB_LOG"
""",
}

# The rendered commands that create the networks, as render.py writes them; the
# instance commands after the marker must not run on an import.
INCUS_COMMANDS = "\n".join(
    [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "# --- Networks ---",
        *(
            f"incus network show {network} >/dev/null 2>&1"
            f" || incus network create {network} --type=bridge"
            for network in NETWORKS
        ),
        "# --- Instances ---",
        "incus init ravensport/fms-01:latest fms-01",
        "",
    ]
)
RENDERED_FILES = ["manifest.json", "incus-commands.sh", "teardown-commands.sh"]


def _teardown_commands(source: str) -> str:
    return f'#!/usr/bin/env bash\necho "teardown from {source}" >>"$STUB_LOG"\n'


def _hwaddr(i: int) -> str:
    return f"00:16:3e:aa:bb:{i:02x}"


def _utc(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _sim_time(moment: datetime) -> str:
    """A time as the sim writes it in a checkpoint, to the nanosecond."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond:06d}000Z"


def _checkpoint(
    age: timedelta = timedelta(seconds=10), supplier: dict | None = None
) -> str:
    """A checkpoint written `age` ago, with the fuel supplier's state
    `supplier` if given."""
    checkpoint = {
        "format_version": 1,
        "written_at": _utc(datetime.now(UTC) - age),
        "entities": {},
    }
    if supplier is not None:
        checkpoint["fuel_supplier"] = supplier
    return json.dumps(checkpoint)


class Deployment:
    """A copy of deploy/ with the snapshot-set scripts, a rendered manifest for
    `NODES`, and stub commands on the path."""

    def __init__(self, path: Path, lima: bool = True, head: str = HEAD):
        self.root = path / "deploy"
        (self.root / "scripts").mkdir(parents=True)
        for script in SCRIPTS:
            shutil.copy(ROOT / "scripts" / script, self.root / "scripts" / script)
        self.build = self.root / "build"
        self.build.mkdir()
        self.write_manifest(self.build, NODES)
        (self.build / "incus-commands.sh").write_text(INCUS_COMMANDS)
        (self.build / "teardown-commands.sh").write_text(_teardown_commands("build"))
        inputs = self.build / "inputs"
        (inputs / "overrides").mkdir(parents=True)
        (inputs / "world.yaml").write_text("# the world rendered on this host\n")
        (inputs / "topology.yaml").write_text("# the topology rendered on this host\n")
        (inputs / "overrides" / "ci-minimal.yaml").write_text("# ci-minimal\n")
        self.snapshots = self.root / "snapshots"

        bin_dir = path / "bin"
        bin_dir.mkdir()
        (bin_dir / "incus").write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{STUB_INCUS}" "$@"\n'
        )
        for name, body in RECORDING_STUBS.items():
            (bin_dir / name).write_text(body)
        for stub in bin_dir.iterdir():
            stub.chmod(0o755)

        self.state_file = path / "incus-state.json"
        self.log_file = path / "commands.log"
        self.log_file.touch()
        self.env = {
            **{
                k: v
                for k, v in os.environ.items()
                if k not in ("INCUS", "SNAPSHOTS_DIR", "PROFILE")
            },
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "STUB_STATE": str(self.state_file),
            "STUB_LOG": str(self.log_file),
            "STUB_LIMA": "1" if lima else "0",
            "STUB_GIT_HEAD": head,
        }
        self.state = {
            "instances": {
                name: {
                    "status": "Running",
                    "snapshots": [],
                    "devices": {},
                    "config": {"volatile.eth0.hwaddr": _hwaddr(i)},
                    "export": f"root filesystem of {name}\n",
                }
                for i, name in enumerate(NODES)
            },
            "networks": list(NETWORKS),
            "pool_total": 10**12,
            "import_network": "vnet-ot",
            "sim": {
                "service": "started",
                "stops_as": "stopped",
                "writes_checkpoint": True,
                "accepts_checkpoint": True,
                "checkpoint": _checkpoint(),
            },
        }

    @staticmethod
    def write_manifest(directory: Path, nodes: list[str]):
        (directory / "manifest.json").write_text(
            json.dumps({"profile": "ci-minimal", "nodes": nodes, "networks": NETWORKS})
        )

    def remove_render(self):
        """Remove every rendered file and input from build/, as on a host
        that has never rendered."""
        for name in RENDERED_FILES:
            (self.build / name).unlink()
        shutil.rmtree(self.build / "inputs")

    @property
    def deployed_set(self) -> str | None:
        pointer = self.snapshots / "deployed-set"
        return pointer.read_text().strip() if pointer.exists() else None

    @property
    def clock_held(self) -> bool:
        return (self.snapshots / "clock-held").exists()

    def hold_clock(self) -> None:
        """Record the clock as held, as a restore leaves it."""
        self.snapshots.mkdir(exist_ok=True)
        (self.snapshots / "clock-held").write_text("2025-03-01T13:00:00Z\n")

    @property
    def state(self) -> dict:
        return json.loads(self.state_file.read_text())

    @state.setter
    def state(self, value: dict):
        self.state_file.write_text(json.dumps(value))

    def update(self, change):
        state = self.state
        change(state)
        self.state = state

    def run(self, script: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(self.root / "scripts" / script), *args],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    @property
    def log(self) -> list[str]:
        return self.log_file.read_text().splitlines()

    def index(self, command: str) -> int:
        """The position of the first logged command starting with `command`."""
        for i, line in enumerate(self.log):
            if line.startswith(command):
                return i
        raise AssertionError(
            f"{command!r} was not run; the log is:\n" + "\n".join(self.log)
        )

    def ran(self, command: str) -> bool:
        return any(line.startswith(command) for line in self.log)

    def changes(self, since: int = 0) -> list[str]:
        """Every logged incus command that changes an instance, from the
        log line `since` onwards."""
        return [
            line
            for line in self.log[since:]
            if line.startswith("incus ")
            and not line.startswith(("incus query ", "incus exec "))
        ]

    def prepare_set(
        self,
        label: str,
        stopped_at: str = "2025-03-01T12:04:00Z",
        checkpoint: str | None = None,
    ) -> Path:
        """A local snapshot set `label`, as snapshot.sh leaves it: every
        instance stopped and snapshotted, the checkpoint and stop time saved."""
        set_dir = self.snapshots / label
        (set_dir / "rendered").mkdir(parents=True)
        (set_dir / "checkpoint.json").write_text(checkpoint or _checkpoint())
        (set_dir / "stopped-at").write_text(stopped_at + "\n")
        for name in RENDERED_FILES:
            shutil.copy(self.build / name, set_dir / "rendered" / name)
        shutil.copytree(self.build / "inputs", set_dir / "inputs")

        def stop_and_snapshot(state):
            for inst in state["instances"].values():
                inst["status"] = "Stopped"
                inst["snapshots"].append(label)
            state["sim"]["service"] = "stopped"

        self.update(stop_and_snapshot)
        return set_dir


@pytest.fixture
def deployment(tmp_path: Path) -> Deployment:
    return Deployment(tmp_path)


def _assert_failed(result: subprocess.CompletedProcess, reason: str):
    assert result.returncode != 0, result.stdout
    assert reason in result.stderr, result.stderr


# ── taking a set (§10.2) ───────────────────────────────────────────────────────


def test_a_set_is_taken_with_the_sim_stopped_first(deployment: Deployment):
    checkpoint = deployment.state["sim"]["checkpoint"]
    result = deployment.run("snapshot.sh", "week-1")
    assert result.returncode == 0, result.stderr

    sim_stop = deployment.index("incus exec sim-host -- rc-service sim stop")
    pull = deployment.index("incus file pull sim-host/run/sim/checkpoint.json")
    sim_host = deployment.index("incus stop sim-host")
    pause = deployment.index("sleep 60")
    stops = [deployment.index(f"incus stop {name}") for name in STOP_ORDER]
    snapshots = [
        deployment.index(f"incus snapshot create {name} week-1") for name in NODES
    ]
    assert sim_stop < pull < sim_host < pause < stops[0]
    assert stops == sorted(stops), "the instances were not stopped in the stop order"
    assert min(snapshots) > stops[-1], (
        "an instance was snapshotted before every instance stopped"
    )

    for name, inst in deployment.state["instances"].items():
        assert inst["status"] == "Stopped", name
        assert inst["snapshots"] == ["week-1"], name
    set_dir = deployment.snapshots / "week-1"
    assert (set_dir / "checkpoint.json").read_text() == checkpoint
    for name in RENDERED_FILES:
        assert (set_dir / "rendered" / name).read_bytes() == (
            deployment.build / name
        ).read_bytes(), name
    assert (set_dir / "inputs" / "world.yaml").read_bytes() == (
        deployment.build / "inputs" / "world.yaml"
    ).read_bytes()
    assert (set_dir / "inputs" / "overrides" / "ci-minimal.yaml").exists()
    stopped_at = (set_dir / "stopped-at").read_text().strip()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stopped_at)
    age = datetime.now(UTC) - datetime.strptime(
        stopped_at, "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=UTC)
    assert timedelta(0) <= age < timedelta(minutes=1)


@pytest.mark.parametrize("label", ["", "../week-1", "-week-1", "week/1", "week 1"])
def test_an_invalid_label_is_refused(deployment: Deployment, label: str):
    result = deployment.run("snapshot.sh", label)
    _assert_failed(result, "LABEL must be")
    assert not deployment.ran("incus ")


def test_an_existing_label_is_refused(deployment: Deployment):
    (deployment.snapshots / "week-1").mkdir(parents=True)
    result = deployment.run("snapshot.sh", "week-1")
    _assert_failed(result, "already exists")
    assert not deployment.changes()
    assert not deployment.ran("incus exec ")
    assert (deployment.snapshots / "week-1").is_dir(), "the existing set was removed"


def test_a_label_an_instance_holds_as_a_snapshot_is_refused(deployment: Deployment):
    """An Incus snapshot left under the label by an earlier set would be taken
    for one of this set's when the set is completed."""
    deployment.update(lambda s: s["instances"]["hist-01"]["snapshots"].append("week-1"))
    result = deployment.run("snapshot.sh", "week-1")
    _assert_failed(result, "hist-01 already holds an Incus snapshot 'week-1'")
    assert not deployment.changes()
    assert not deployment.ran("incus exec ")
    assert not (deployment.snapshots / "week-1").exists()


def test_a_set_is_not_taken_from_a_partly_stopped_environment(deployment: Deployment):
    deployment.update(lambda s: s["instances"]["hist-01"].update(status="Stopped"))
    result = deployment.run("snapshot.sh", "week-1")
    _assert_failed(result, "hist-01 is not running")
    assert not deployment.changes()
    assert not deployment.ran("incus exec ")
    assert not (deployment.snapshots / "week-1").exists()


@pytest.mark.parametrize(
    ("sim", "reason"),
    [
        ({"stops_as": "started"}, "the sim service did not stop"),
        (
            {"stops_as": "crashed"},
            "a sim that crashed or was killed wrote no checkpoint",
        ),
        ({"writes_checkpoint": False}, "the sim stopped but wrote no checkpoint"),
    ],
)
def test_a_sim_that_did_not_come_to_rest_leaves_no_set(
    deployment: Deployment, sim: dict, reason: str
):
    deployment.update(lambda s: s["sim"].update(sim))
    result = deployment.run("snapshot.sh", "week-1")
    _assert_failed(result, reason)
    assert not deployment.changes(), "an instance was stopped or snapshotted"
    assert not (deployment.snapshots / "week-1").exists()


@pytest.mark.parametrize("service", ["crashed", "stopped"])
def test_a_set_is_not_taken_while_the_sim_is_not_running(
    deployment: Deployment, service: str
):
    """A sim that has already exited cannot come to rest, so it would write no
    checkpoint; the set is refused before anything is stopped."""
    deployment.update(lambda s: s["sim"].update(service=service))
    result = deployment.run("snapshot.sh", "week-1")
    _assert_failed(
        result, f"the sim service on sim-host is not running but ' * status: {service}'"
    )
    assert not deployment.changes()
    assert not deployment.ran("incus exec sim-host -- rc-service sim stop")
    assert not (deployment.snapshots / "week-1").exists()


def test_a_set_is_not_taken_without_the_render_inputs(deployment: Deployment):
    """A deployment rendered before render.py recorded its inputs cannot say
    how it was rendered."""
    shutil.rmtree(deployment.build / "inputs")
    result = deployment.run("snapshot.sh", "week-1")
    _assert_failed(result, "render.py recorded its inputs")
    assert not deployment.changes()
    assert not (deployment.snapshots / "week-1").exists()


def test_a_set_taken_from_a_restored_deployment_keeps_its_rendered_files(
    deployment: Deployment,
):
    """The instances of a restored run come from the set, not from the
    local render, so a set taken from them carries the set's files."""
    set_dir = deployment.prepare_set("week-1")
    (set_dir / "rendered" / "teardown-commands.sh").write_text(
        _teardown_commands("week-1")
    )
    assert deployment.run("restore.sh", "week-1").returncode == 0
    (deployment.build / "teardown-commands.sh").write_text(
        _teardown_commands("a later render")
    )
    deployment.update(lambda s: s["sim"].update(service="started"))
    result = deployment.run("snapshot.sh", "week-2")
    assert result.returncode == 0, result.stderr
    assert (
        deployment.snapshots / "week-2" / "rendered" / "teardown-commands.sh"
    ).read_text() == (_teardown_commands("week-1"))


# ── a failed set and its completion (§10.2) ───────────────────────────────────

# The failures a set can meet once the sim's checkpoint is confirmed on
# sim-host: each as the change to the stub's state that causes it, and the
# change that removes the cause.
FAILURES = {
    "the checkpoint cannot be copied": (
        lambda s: s["sim"].update(pull_fails=True),
        lambda s: s["sim"].update(pull_fails=False),
    ),
    "an instance does not stop": (
        lambda s: s["instances"]["hist-01"].update(hangs_on_stop=True),
        lambda s: s["instances"]["hist-01"].update(hangs_on_stop=False),
    ),
    "the storage pool is full": (
        lambda s: s["instances"]["hmi-01"].update(snapshot_fails=True),
        lambda s: s["instances"]["hmi-01"].update(snapshot_fails=False),
    ),
}


def _fail_set(deployment: Deployment, failure: str) -> subprocess.CompletedProcess:
    """Take the set week-1 with `failure` caused, then remove the cause."""
    cause, removal = FAILURES[failure]
    deployment.update(cause)
    result = deployment.run("snapshot.sh", "week-1")
    assert result.returncode != 0, result.stdout
    deployment.update(removal)
    return result


@pytest.mark.parametrize("failure", FAILURES)
def test_a_failure_once_the_checkpoint_is_confirmed_keeps_the_set(
    deployment: Deployment, failure: str
):
    """From the confirmation on, the checkpoint is the only record of the
    sim's state, so a failure keeps the set, marked incomplete."""
    checkpoint = deployment.state["sim"]["checkpoint"]
    result = _fail_set(deployment, failure)
    assert "complete it with: make -C deploy snapshot LABEL=week-1" in result.stderr

    set_dir = deployment.snapshots / "week-1"
    assert (set_dir / "incomplete").exists()
    for name in RENDERED_FILES:
        assert (set_dir / "rendered" / name).exists(), name
    if failure == "the checkpoint cannot be copied":
        assert not (set_dir / "checkpoint.json").exists()
    else:
        assert (set_dir / "checkpoint.json").read_text() == checkpoint
    if failure == "the storage pool is full":
        # The snapshots taken before the failure are kept.
        snapshots = {
            name: inst["snapshots"]
            for name, inst in deployment.state["instances"].items()
        }
        assert snapshots == {
            "fw-ot-dmz": ["week-1"],
            "hist-01": ["week-1"],
            "fms-01": ["week-1"],
            "hmi-01": [],
            "sim-host": [],
        }


@pytest.mark.parametrize("failure", FAILURES)
def test_an_incomplete_set_is_completed_by_taking_it_again(
    deployment: Deployment, failure: str
):
    """Taking the set again repeats only what the failure left undone, from
    the set's own instance list, and the completed set restores."""
    checkpoint = deployment.state["sim"]["checkpoint"]
    _fail_set(deployment, failure)
    set_dir = deployment.snapshots / "week-1"
    stopped_at = (
        (set_dir / "stopped-at").read_text()
        if (set_dir / "stopped-at").exists()
        else None
    )
    # A render made since the failure does not change the set's instances:
    # new-node-01 has no place in the stop order and no instance.
    deployment.write_manifest(deployment.build, [*NODES, "new-node-01"])

    result = deployment.run("snapshot.sh", "week-1")
    assert result.returncode == 0, result.stderr

    assert not (set_dir / "incomplete").exists()
    assert (set_dir / "checkpoint.json").read_text() == checkpoint
    if stopped_at is not None:
        assert (set_dir / "stopped-at").read_text() == stopped_at, (
            "the recorded stop time was changed"
        )
    # The stub refuses to stop a stopped instance or to create a snapshot that
    # exists, so the success above shows neither was repeated.
    for name, inst in deployment.state["instances"].items():
        assert inst["status"] == "Stopped", name
        assert inst["snapshots"] == ["week-1"], name
    sim_stops = [
        line
        for line in deployment.log
        if line.startswith("incus exec sim-host -- rc-service sim stop")
    ]
    assert len(sim_stops) == 1, "the sim was stopped a second time"
    assert not deployment.ran("incus stop new-node-01")

    assert deployment.run("restore.sh", "week-1").returncode == 0


# Sets that cannot be completed: the failure, the change made to the
# environment since, and the reason the completion gives.
CANNOT_COMPLETE = {
    "the sim has been started again": (
        "the checkpoint cannot be copied",
        lambda s: s["sim"].update(service="started"),
        "the sim service on sim-host is ' * status: started', not stopped",
    ),
    "the checkpoint has been lost with sim-host": (
        "the checkpoint cannot be copied",
        lambda s: s["instances"]["sim-host"].update(status="Stopped"),
        "the set lacks the sim's checkpoint and sim-host no longer holds it",
    ),
    "an instance has been started since the stop time": (
        "the storage pool is full",
        lambda s: s["instances"]["hist-01"].update(status="Running"),
        "hist-01 has been started since the set's stop time",
    ),
    # A restore of another set followed by make stop leaves every instance
    # stopped, as the failure did; only its last start shows that it has run.
    "an instance has been started and stopped again": (
        "the storage pool is full",
        lambda s: s["instances"]["hmi-01"].update(last_used_at="2025-03-02T09:00:00Z"),
        "hmi-01 has been started since the set was begun",
    ),
    "an instance has been deleted": (
        "the storage pool is full",
        lambda s: s["instances"].pop("hist-01"),
        "hist-01 does not exist",
    ),
}


@pytest.mark.parametrize("case", CANNOT_COMPLETE)
def test_a_set_whose_environment_has_changed_since_cannot_be_completed(
    deployment: Deployment, case: str
):
    failure, change, reason = CANNOT_COMPLETE[case]
    _fail_set(deployment, failure)
    deployment.update(change)
    before = len(deployment.log)

    result = deployment.run("snapshot.sh", "week-1")
    _assert_failed(result, reason)
    assert "the set cannot be completed" in result.stderr
    assert "complete it with" not in result.stderr
    assert not deployment.changes(since=before)
    assert (deployment.snapshots / "week-1" / "incomplete").exists()


def test_a_set_that_no_restore_could_start_is_kept_incomplete(
    deployment: Deployment,
):
    """A restore sets the clock to the stop time plus five seconds, and the
    sim refuses a checkpoint written more than five minutes before it starts,
    so a set stopped 4 min 58 s after its checkpoint can never be restored. It
    is refused before any instance is snapshotted, and taking it again fails
    in the same way."""
    deployment.update(
        lambda s: s["sim"].update(checkpoint=_checkpoint(age=timedelta(seconds=298)))
    )
    result = deployment.run("snapshot.sh", "week-1")
    _assert_failed(result, "no restore of the set can start the sim")
    assert "the set cannot be completed" in result.stderr
    assert not deployment.ran("incus snapshot create")
    for name, inst in deployment.state["instances"].items():
        assert inst["status"] == "Stopped", name
    set_dir = deployment.snapshots / "week-1"
    assert (set_dir / "incomplete").exists()
    assert (set_dir / "stopped-at").exists()

    before = len(deployment.log)
    result = deployment.run("snapshot.sh", "week-1")
    _assert_failed(result, "no restore of the set can start the sim")
    assert not deployment.changes(since=before)


POLL_WARNING = "will probably stop the sim within seconds of every restore"

# The fuel supplier's next poll, as a time from the checkpoint, whether a
# tanker is due at once, and whether the set is taken with a warning. A restore
# sets the clock to the stop time plus five seconds, and a poll due less than
# 60 seconds after that is made at the start, while atg-relay-01 still refuses
# connections (§10.2). Polls fall due whether or not an order is pending.
SUPPLIER_STATES = {
    "a poll due before a restore's clock time": (timedelta(seconds=-10), False, True),
    "a poll due within 60 seconds of it": (timedelta(seconds=40), False, True),
    "a poll due later": (timedelta(minutes=10), False, False),
    "a poll due within 60 seconds, with a tanker due": (
        timedelta(seconds=40),
        True,
        True,
    ),
    "a poll due later, with a tanker due": (timedelta(minutes=10), True, False),
}


def _supplier(deployment: Deployment, case: str) -> bool:
    """Give the sim's checkpoint the fuel supplier state `case`; whether the
    set should be taken with a warning."""
    offset, tanker_due, warns = SUPPLIER_STATES[case]
    now = datetime.now(UTC)
    order = (
        {"TankerDueAt": _sim_time(now - timedelta(seconds=10))}
        if tanker_due
        else "None"
    )
    supplier = {"next_poll_at": _sim_time(now + offset), "order": order}
    deployment.update(
        lambda s: s["sim"].update(checkpoint=_checkpoint(supplier=supplier))
    )
    return warns


@pytest.mark.parametrize("case", SUPPLIER_STATES)
def test_a_set_whose_supplier_poll_falls_at_a_restores_start_is_taken_with_a_warning(
    deployment: Deployment, case: str
):
    """Such a set stops the sim within seconds of every restore, but its
    history cannot be taken again, so it is kept and the target warns."""
    warns = _supplier(deployment, case)
    result = deployment.run("snapshot.sh", "week-1")
    assert result.returncode == 0, result.stderr
    assert not (deployment.snapshots / "week-1" / "incomplete").exists()
    for name, inst in deployment.state["instances"].items():
        assert inst["snapshots"] == ["week-1"], name
    assert (POLL_WARNING in result.stderr) == warns, result.stderr
    if warns:
        assert "do not export it" in result.stderr


def test_completing_a_set_warns_of_a_supplier_poll_at_a_restores_start(
    deployment: Deployment,
):
    """The warning is given once the set is complete, so a failed attempt
    gives none and the completion does."""
    _supplier(deployment, "a poll due within 60 seconds of it")
    failed = _fail_set(deployment, "the storage pool is full")
    assert POLL_WARNING not in failed.stderr
    result = deployment.run("snapshot.sh", "week-1")
    assert result.returncode == 0, result.stderr
    assert POLL_WARNING in result.stderr


# ── restoring a set (§8.1, §8.2) ───────────────────────────────────────────────


def test_a_restore_starts_a_run_from_the_set(deployment: Deployment):
    set_dir = deployment.prepare_set("week-1", stopped_at="2025-03-01T12:04:00Z")
    result = deployment.run("restore.sh", "week-1")
    assert result.returncode == 0, result.stderr

    # Every instance is restored, then the clock is held and set, before any
    # instance starts.
    restores = [
        deployment.index(f"incus snapshot restore {name} week-1") for name in NODES
    ]
    hold = deployment.index("sudo timedatectl set-ntp false")
    agent = deployment.index("sudo systemctl restart lima-guestagent")
    clock = deployment.index("sudo date -u -s 2025-03-01T12:04:05Z")
    starts = [deployment.index(f"incus start {name}") for name in reversed(STOP_ORDER)]
    assert max(restores) < hold < agent < clock < min(starts)
    assert deployment.index("tee> CapabilityBoundingSet=~CAP_SYS_TIME") < agent
    assert deployment.ran(f"sudo tee {DROPIN}")

    # The instances start in the reverse of the stop order, the sim last, once
    # the FMS, the HMI and the directory on corp-it are ready and its
    # checkpoint is mounted.
    assert starts == sorted(starts)
    mount = deployment.index("incus config device add sim-host sim-state disk")
    sim_start = deployment.index("incus start sim-host")
    ready = max(
        deployment.index(
            "incus exec fms-01 -- curl -fsk --max-time 3 https://127.0.0.1/api/v1/health"
        ),
        deployment.index(
            "incus exec fms-01 -- curl -fsk --max-time 3 https://10.40.0.12/health"
        ),
        deployment.index(
            "incus exec fms-01 -- timeout 3 openssl s_client -connect corp-it:636 -brief"
        ),
    )
    assert max(starts) < ready < sim_start
    assert mount < sim_start
    loaded = deployment.index(
        "incus exec sim-host -- test -e /run/sim/checkpoint-loaded"
    )
    assert loaded > sim_start

    # The sim is confirmed to be running 30 seconds after it accepted the
    # checkpoint, by which time a fault in its first connections would have
    # stopped it (§8.1, step 9).
    after = deployment.log[loaded:]
    sleeps = [i for i, line in enumerate(after) if re.fullmatch(r"sleep \d+", line)]
    assert sum(int(after[i].split()[1]) for i in sleeps) >= 30
    statuses = [
        i
        for i, line in enumerate(after)
        if line == "incus exec sim-host -- rc-service sim status"
    ]
    assert statuses and max(statuses) > max(sleeps)

    # The mount directory holds a copy of the checkpoint and nothing else.
    mount_dir = set_dir / "mount"
    assert sorted(p.name for p in mount_dir.iterdir()) == ["checkpoint.json"]
    assert (mount_dir / "checkpoint.json").read_text() == (
        set_dir / "checkpoint.json"
    ).read_text()
    assert mount_dir.stat().st_mode & 0o777 == 0o755
    assert (mount_dir / "checkpoint.json").stat().st_mode & 0o777 == 0o644
    added = deployment.log[mount].split()
    assert f"source={mount_dir.resolve()}" in added
    assert "path=/var/lib/sim/state" in added
    assert "readonly=true" in added

    # The device is removed once the sim has accepted the checkpoint, so that a
    # later restart of sim-host does not reuse it.
    state = deployment.state
    assert "sim-state" not in state["instances"]["sim-host"]["devices"]
    for name, inst in state["instances"].items():
        assert inst["status"] == "Running", name

    # The run log records the run by the real time of the restore, not
    # the set's stop time, and its seed.
    ((logged_at, seed),) = [
        line.split() for line in (set_dir / "runs.log").read_text().splitlines()
    ]
    started = datetime.strptime(logged_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs(datetime.now(UTC) - started) < timedelta(minutes=1)
    assert seed == "seed=none"


def test_a_restore_off_lima_holds_only_the_time_synchronisation(tmp_path: Path):
    deployment = Deployment(tmp_path, lima=False)
    deployment.prepare_set("week-1")
    result = deployment.run("restore.sh", "week-1")
    assert result.returncode == 0, result.stderr
    assert deployment.ran("sudo timedatectl set-ntp false")
    assert not deployment.ran("sudo tee")
    assert not deployment.ran("sudo systemctl")


def test_a_seed_is_written_as_a_reseed_file(deployment: Deployment):
    set_dir = deployment.prepare_set("week-1")
    result = deployment.run("restore.sh", "week-1", "42")
    assert result.returncode == 0, result.stderr
    reseed = set_dir / "mount" / "reseed.json"
    assert json.loads(reseed.read_text()) == {"tug_arrival_seed": 42}
    assert reseed.stat().st_mode & 0o777 == 0o644
    assert (set_dir / "runs.log").read_text().split()[1] == "seed=42"


def test_a_restore_without_a_seed_drops_an_earlier_reseed_file(
    deployment: Deployment,
):
    """The mount directory is refilled by every restore, so a seed given to
    one run is not applied to the next, and the stored checkpoint is never
    modified."""
    set_dir = deployment.prepare_set("week-1")
    checkpoint = (set_dir / "checkpoint.json").read_bytes()
    assert deployment.run("restore.sh", "week-1", "42").returncode == 0
    assert deployment.run("stop.sh").returncode == 0
    result = deployment.run("restore.sh", "week-1")
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in (set_dir / "mount").iterdir()) == ["checkpoint.json"]
    assert (set_dir / "checkpoint.json").read_bytes() == checkpoint
    seeds = [
        line.split()[1] for line in (set_dir / "runs.log").read_text().splitlines()
    ]
    assert seeds == ["seed=42", "seed=none"]


@pytest.mark.parametrize("seed", ["-1", "abc", "1.5", "1" * 21])
def test_an_invalid_seed_is_refused(deployment: Deployment, seed: str):
    deployment.prepare_set("week-1")
    result = deployment.run("restore.sh", "week-1", seed)
    _assert_failed(result, "SEED must be a non-negative integer")
    assert not deployment.ran("incus ")
    assert not deployment.ran("sudo ")


def test_a_restore_is_refused_while_an_instance_runs(deployment: Deployment):
    deployment.prepare_set("week-1")
    deployment.update(lambda s: s["instances"]["hmi-01"].update(status="Running"))
    result = deployment.run("restore.sh", "week-1")
    _assert_failed(
        result,
        "hmi-01 is running; stop the current restored run first with make -C deploy stop",
    )
    assert not deployment.changes()
    assert not deployment.ran("sudo ")


def test_a_restore_is_refused_when_an_instance_lacks_the_snapshot(
    deployment: Deployment,
):
    deployment.prepare_set("week-1")
    deployment.update(lambda s: s["instances"]["fms-01"].update(snapshots=[]))
    result = deployment.run("restore.sh", "week-1")
    _assert_failed(result, "fms-01 has no snapshot 'week-1'")
    assert not deployment.changes()
    assert not deployment.ran("sudo ")


def test_a_restore_of_an_unknown_set_is_refused(deployment: Deployment):
    deployment.prepare_set("week-1")
    result = deployment.run("restore.sh", "week-2")
    _assert_failed(result, "'week-2' is not a snapshot set")
    assert not deployment.changes()
    assert not deployment.ran("sudo ")


def test_an_incomplete_set_is_not_restored(deployment: Deployment):
    set_dir = deployment.prepare_set("week-1")
    (set_dir / "incomplete").touch()
    result = deployment.run("restore.sh", "week-1")
    _assert_failed(
        result,
        "'week-1' is an incomplete snapshot set;"
        " complete it with make -C deploy snapshot LABEL=week-1",
    )
    assert not deployment.changes()
    assert not deployment.ran("sudo ")


def test_no_set_is_restored_while_another_is_incomplete(deployment: Deployment):
    """A restore replaces the instance state an incomplete set is completed
    from, so it is refused until that set is completed or discarded."""
    deployment.prepare_set("week-1")
    (deployment.prepare_set("week-2") / "incomplete").touch()
    result = deployment.run("restore.sh", "week-1")
    _assert_failed(
        result,
        "the snapshot set 'week-2' is incomplete, and a restore would replace"
        " the instance state it is completed from",
    )
    assert "make -C deploy snapshot LABEL=week-2" in result.stderr
    assert not deployment.changes()
    assert not deployment.ran("sudo ")


def test_the_sim_is_not_started_from_a_checkpoint_it_would_refuse(
    deployment: Deployment,
):
    """The sim refuses a checkpoint written more than five minutes before it
    starts (sim.md §9.3); a restore that took longer leaves sim-host stopped."""
    deployment.prepare_set("week-1", checkpoint=_checkpoint(age=timedelta(minutes=6)))
    result = deployment.run("restore.sh", "week-1")
    _assert_failed(result, "the restore took too long")
    assert not deployment.ran("incus start sim-host")
    assert deployment.state["instances"]["sim-host"]["status"] == "Stopped"


def test_a_refused_checkpoint_fails_the_restore_with_the_sim_log(
    deployment: Deployment,
):
    deployment.prepare_set("week-1")
    deployment.update(lambda s: s["sim"].update(accepts_checkpoint=False))
    result = deployment.run("restore.sh", "week-1")
    _assert_failed(result, "the sim did not accept the checkpoint within 60s")
    assert "sim: the checkpoint was refused" in result.stderr
    waits = [line for line in deployment.log if line == "sleep 1"]
    assert len(waits) == 60
    assert not (deployment.snapshots / "week-1" / "runs.log").exists(), (
        "a run the sim did not start was recorded"
    )


def test_a_sim_that_stops_soon_after_accepting_its_checkpoint_fails_the_restore(
    deployment: Deployment,
):
    """The sim acts at once on every due time that has passed (sim.md §9.3),
    so a fault in its first connections, such as a fuel supplier poll refused
    at the relay, stops it within seconds of accepting the checkpoint. A
    restore that left it stopped would report a run that is not running
    (§8.1, step 9)."""
    deployment.prepare_set("week-1")
    deployment.update(
        lambda s: s["sim"].update(
            stops_after_start=True,
            log_tail="fuel_supplier: ATG poll failed after 3 retries: early eof",
        )
    )
    result = deployment.run("restore.sh", "week-1")
    _assert_failed(result, "the sim stopped within 30s of accepting the checkpoint")
    assert "fuel_supplier: ATG poll failed after 3 retries: early eof" in result.stderr
    assert not (deployment.snapshots / "week-1" / "runs.log").exists(), (
        "a run the sim did not keep running was recorded"
    )
    assert deployment.clock_held


def test_a_sim_state_device_left_by_an_earlier_restore_is_replaced(
    deployment: Deployment,
):
    set_dir = deployment.prepare_set("week-1")
    stale = {
        "type": "disk",
        "source": "/nonexistent",
        "path": "/var/lib/sim/state",
        "readonly": "true",
    }
    deployment.update(
        lambda s: s["instances"]["sim-host"]["devices"].update({"sim-state": stale})
    )
    result = deployment.run("restore.sh", "week-1")
    assert result.returncode == 0, result.stderr
    assert (
        f"source={(set_dir / 'mount').resolve()}"
        in deployment.log[
            deployment.index("incus config device add sim-host sim-state disk")
        ]
    )


def test_a_restore_reads_only_the_set(deployment: Deployment):
    """A restore takes its instance list from the set's rendered manifest, not
    from build/, and records that the deployed instances are the set's."""
    deployment.prepare_set("week-1")
    deployment.write_manifest(
        deployment.build, ["fw-ot-dmz", "fms-01", "hmi-01", "sim-host"]
    )
    shutil.rmtree(deployment.build / "inputs")
    result = deployment.run("restore.sh", "week-1")
    assert result.returncode == 0, result.stderr
    assert deployment.ran("incus snapshot restore hist-01 week-1")
    assert deployment.ran("incus start hist-01")
    assert not deployment.ran("make")
    assert deployment.deployed_set == "week-1"


def test_the_restored_set_describes_the_deployment_until_it_is_torn_down(
    deployment: Deployment,
):
    """After a restore, make stop and make down use the set's
    rendered files even when build/ has been rendered again."""
    set_dir = deployment.prepare_set("week-1")
    (set_dir / "rendered" / "teardown-commands.sh").write_text(
        _teardown_commands("week-1")
    )
    assert deployment.run("restore.sh", "week-1").returncode == 0
    deployment.write_manifest(
        deployment.build, ["fw-ot-dmz", "fms-01", "hmi-01", "sim-host"]
    )

    assert deployment.run("stop.sh").returncode == 0
    assert deployment.ran("incus stop --timeout 120 hist-01")

    result = deployment.run("teardown.sh", "1")
    assert result.returncode == 0, result.stderr
    assert deployment.ran("teardown from week-1")
    assert not deployment.ran("teardown from build")
    assert deployment.deployed_set is None


# ── stopping a restored run (§8.2) ─────────────────────────────────────────────


def test_stopping_a_restored_run_stops_every_instance_before_the_clock_is_released(
    deployment: Deployment,
):
    deployment.hold_clock()
    result = deployment.run("stop.sh")
    assert result.returncode == 0, result.stderr

    sim_host = deployment.index("incus stop --force sim-host")
    stops = [
        deployment.index(f"incus stop --timeout 120 {name}") for name in STOP_ORDER
    ]
    assert sim_host < stops[0]
    assert stops == sorted(stops)
    release = [
        deployment.index(f"sudo rm -f {DROPIN}"),
        deployment.index("sudo systemctl daemon-reload"),
        deployment.index("sudo systemctl restart lima-guestagent"),
        deployment.index("sudo timedatectl set-ntp true"),
    ]
    assert stops[-1] < min(release)
    assert release == sorted(release)
    for name, inst in deployment.state["instances"].items():
        assert inst["status"] == "Stopped", name


def test_stopping_a_restored_run_skips_stopped_instances(deployment: Deployment):
    def stop(state):
        state["instances"]["sim-host"]["status"] = "Stopped"
        state["instances"]["hist-01"]["status"] = "Stopped"

    deployment.update(stop)
    deployment.hold_clock()
    result = deployment.run("stop.sh")
    assert result.returncode == 0, result.stderr
    assert not deployment.ran("incus stop --force sim-host")
    assert not deployment.ran("incus stop --timeout 120 hist-01")
    assert deployment.ran("sudo timedatectl set-ntp true")


def test_stopping_a_restored_run_forces_an_instance_that_does_not_stop(
    deployment: Deployment,
):
    deployment.hold_clock()
    deployment.update(lambda s: s["instances"]["hist-01"].update(hangs_on_stop=True))
    result = deployment.run("stop.sh")
    assert result.returncode == 0, result.stderr
    assert (
        "WARNING: hist-01 did not stop within 120s; forcing it to stop" in result.stderr
    )
    assert (
        deployment.index("incus stop --timeout 120 hist-01")
        < deployment.index("incus stop --force hist-01")
        < deployment.index("incus stop --timeout 120 fw-ot-dmz")
    )
    assert not deployment.ran("incus stop --force fms-01")
    for name, inst in deployment.state["instances"].items():
        assert inst["status"] == "Stopped", name
    assert deployment.ran("sudo timedatectl set-ntp true")


def test_make_stop_changes_nothing_without_a_restored_run(deployment: Deployment):
    """A deployment created by make deploy is not a restored run: its sim is
    not force-stopped and the host clock is left alone."""
    result = deployment.run("stop.sh")
    _assert_failed(result, "no restored run is in progress")
    assert (
        "applies only to an environment restored from a snapshot set" in result.stderr
    )
    assert not deployment.changes()
    assert not deployment.ran("sudo ")
    for name, inst in deployment.state["instances"].items():
        assert inst["status"] == "Running", name


def test_a_set_is_not_taken_by_forcing_an_instance_to_stop(deployment: Deployment):
    """A forced stop is a power loss to the instance's data stores; a set is
    taken only from clean stops, however long they take."""
    result = deployment.run("snapshot.sh", "week-1")
    assert result.returncode == 0, result.stderr
    assert not deployment.ran("incus stop --force")
    assert not deployment.ran("incus stop --timeout")


def test_the_clock_is_held_from_the_restore_until_the_run_is_stopped(
    deployment: Deployment,
):
    deployment.prepare_set("week-1")
    assert deployment.run("restore.sh", "week-1").returncode == 0
    assert deployment.clock_held
    assert deployment.run("stop.sh").returncode == 0
    assert not deployment.clock_held


# ── removing the deployment (§8) ───────────────────────────────────────────────


def test_make_down_refuses_while_a_restored_run_is_in_progress(deployment: Deployment):
    """Deleting the instances of a restored run would leave the host clock
    held; stopping the run keeps the instances and their sets."""
    deployment.prepare_set("week-1")
    assert deployment.run("restore.sh", "week-1").returncode == 0
    before = len(deployment.log)
    result = deployment.run("teardown.sh")
    _assert_failed(result, "a restored run is in progress")
    assert "make -C deploy stop" in result.stderr
    assert not any(
        line.startswith(("teardown from", "sudo ")) for line in deployment.log[before:]
    )
    assert deployment.clock_held
    assert deployment.deployed_set == "week-1"


def test_make_down_refuses_while_an_instance_holds_a_snapshot_set(
    deployment: Deployment,
):
    """Deleting an instance deletes its snapshots, so the refusal names each
    set and whether it can still be restored from an exported file."""
    deployment.prepare_set("week-1")
    deployment.prepare_set("week-2")
    file = _export(deployment)
    result = deployment.run("teardown.sh")
    _assert_failed(result, "nothing was changed; run make -C deploy down FORCE=1")
    assert f"week-1: exported to {file.relative_to(deployment.root)}" in result.stderr
    assert (
        "week-2: not exported (make -C deploy export-snapshot LABEL=week-2)"
        in result.stderr
    )
    assert not deployment.ran("teardown from")


def test_make_down_names_an_incomplete_set_with_the_command_that_completes_it(
    deployment: Deployment,
):
    """An incomplete set cannot be exported, so the refusal gives the command
    that completes it rather than the export."""
    set_dir = deployment.prepare_set("week-1")
    (set_dir / "incomplete").touch()
    result = deployment.run("teardown.sh")
    _assert_failed(result, "nothing was changed; run make -C deploy down FORCE=1")
    assert "week-1: incomplete (make -C deploy snapshot LABEL=week-1)" in result.stderr
    assert "export-snapshot LABEL=week-1" not in result.stderr
    assert not deployment.ran("teardown from")


def test_make_down_names_an_incomplete_set_no_instance_holds_a_snapshot_of(
    deployment: Deployment,
):
    """The checkpoint could not be copied, so the set holds no Incus snapshot
    and its checkpoint is held only on sim-host; deleting the instances would
    lose it."""
    _fail_set(deployment, "the checkpoint cannot be copied")
    assert not any(inst["snapshots"] for inst in deployment.state["instances"].values())
    result = deployment.run("teardown.sh")
    _assert_failed(result, "nothing was changed; run make -C deploy down FORCE=1")
    assert "week-1: incomplete (make -C deploy snapshot LABEL=week-1)" in result.stderr
    assert not deployment.ran("teardown from")


def test_make_down_refuses_when_incus_cannot_be_queried(deployment: Deployment):
    deployment.update(lambda s: s.update(unreachable=True))
    result = deployment.run("teardown.sh")
    _assert_failed(result, "Incus cannot be queried")
    assert not deployment.ran("teardown from")


def test_make_down_tears_down_a_deployment_without_sets_or_a_restored_run(
    deployment: Deployment,
):
    result = deployment.run("teardown.sh")
    assert result.returncode == 0, result.stderr
    assert deployment.ran("teardown from build")
    assert not deployment.ran("sudo ")


def test_a_forced_make_down_deletes_the_sets_and_releases_the_clock(
    deployment: Deployment,
):
    """The step back to real time is made after every instance is deleted, so
    that no host records it."""
    set_dir = deployment.prepare_set("week-1")
    assert deployment.run("restore.sh", "week-1").returncode == 0
    result = deployment.run("teardown.sh", "1")
    assert result.returncode == 0, result.stderr
    assert deployment.index("teardown from build") < deployment.index(
        "sudo timedatectl set-ntp true"
    )
    assert not deployment.clock_held
    assert deployment.deployed_set is None
    assert (set_dir / "runs.log").exists(), "the set's records were deleted"


def test_an_invalid_force_is_refused(deployment: Deployment):
    result = deployment.run("teardown.sh", "yes")
    _assert_failed(result, "FORCE must be 1")
    assert not deployment.ran("teardown from")


def test_make_deploy_refuses_while_a_restored_run_is_in_progress(
    deployment: Deployment,
):
    """Instances started under a held clock would record the snapshot time."""
    deployment.prepare_set("week-1")
    assert deployment.run("restore.sh", "week-1").returncode == 0
    before = len(deployment.log)
    result = deployment.run("deploy.sh")
    _assert_failed(result, "a restored run holds the host clock")
    assert deployment.log[before:] == []
    assert deployment.deployed_set == "week-1"


# ── moving a set between hosts (§10.3, §10.4) ──────────────────────────────────


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _export(deployment: Deployment, label: str = "week-1") -> Path:
    result = deployment.run("export-snapshot.sh", label)
    assert result.returncode == 0, result.stderr
    return (
        deployment.snapshots
        / f"ravensport-snapshot-{label}-ci-minimal-{platform.machine()}.tar"
    )


def test_an_export_writes_one_file_holding_the_set(
    deployment: Deployment, tmp_path: Path
):
    set_dir = deployment.prepare_set("week-1")
    deployment.update(lambda s: s["instances"]["fms-01"].update(export="x" * 5000))
    file = _export(deployment)

    # Each instance is exported from its snapshot, through a temporary copy
    # that is deleted afterwards.
    for name in NODES:
        copy = deployment.index(f"incus copy {name}/week-1 snapshot-export-{name}")
        assert copy < deployment.index(f"incus export snapshot-export-{name} ")
    assert sorted(deployment.state["instances"]) == sorted(NODES)
    assert not (deployment.snapshots / ".export-week-1").exists()

    # A copy is given new MAC addresses; each export carries the snapshot's,
    # so that the restored hosts keep the producer's.
    for i, name in enumerate(NODES):
        assert deployment.state["exported_config"][f"snapshot-export-{name}"] == {
            "volatile.eth0.hwaddr": _hwaddr(i)
        }, name

    unpacked = tmp_path / "unpacked"
    with tarfile.open(file) as archive:
        archive.extractall(unpacked, filter="data")
    manifest = json.loads((unpacked / "manifest.json").read_text())
    assert manifest["format_version"] == 2
    assert manifest["label"] == "week-1"
    assert manifest["profile"] == "ci-minimal"
    assert manifest["arch"] == platform.machine()
    assert manifest["commit"] == HEAD
    assert manifest["incus_version"] == "6.0.0"
    assert manifest["stopped_at"] == "2025-03-01T12:04:00Z"
    assert [entry["name"] for entry in manifest["instances"]] == NODES
    for entry in manifest["instances"]:
        archive = unpacked / entry["archive"]
        assert entry["archive"] == f"instances/{entry['name']}.tar.zst"
        contents = subprocess.run(
            ["zstd", "-dc", str(archive)], capture_output=True, check=True
        ).stdout
        assert (
            contents == deployment.state["instances"][entry["name"]]["export"].encode()
        )
        assert entry["sha256"] == _sha256(archive)
        # The disk usage is the uncompressed size of the export.
        assert entry["disk_usage_bytes"] == len(contents)
    assert (unpacked / "checkpoint.json").read_bytes() == (
        set_dir / "checkpoint.json"
    ).read_bytes()
    assert manifest["checkpoint_sha256"] == _sha256(unpacked / "checkpoint.json")
    assert manifest["stopped_at_sha256"] == _sha256(unpacked / "stopped-at")

    # The set's rendered files and inputs, each with its digest.
    files = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    assert sorted(files) == sorted(
        [f"rendered/{name}" for name in RENDERED_FILES]
        + [
            "inputs/overrides/ci-minimal.yaml",
            "inputs/topology.yaml",
            "inputs/world.yaml",
        ]
    )
    for path, sha256 in files.items():
        assert (unpacked / path).read_bytes() == (set_dir / path).read_bytes(), path
        assert sha256 == _sha256(unpacked / path), path


def test_an_export_is_refused_when_an_instance_lacks_the_snapshot(
    deployment: Deployment,
):
    deployment.prepare_set("week-1")
    deployment.update(lambda s: s["instances"]["hist-01"].update(snapshots=[]))
    result = deployment.run("export-snapshot.sh", "week-1")
    _assert_failed(result, "hist-01 has no snapshot 'week-1'")
    assert not deployment.changes()


def test_an_export_of_an_unknown_set_is_refused(deployment: Deployment):
    result = deployment.run("export-snapshot.sh", "week-1")
    _assert_failed(result, "'week-1' is not a snapshot set")
    assert not deployment.ran("incus ")


def test_an_incomplete_set_is_not_exported(deployment: Deployment):
    set_dir = deployment.prepare_set("week-1")
    (set_dir / "incomplete").touch()
    result = deployment.run("export-snapshot.sh", "week-1")
    _assert_failed(result, "'week-1' is an incomplete snapshot set")
    assert not deployment.ran("incus ")
    assert not list(deployment.snapshots.glob("*.tar"))


def _fresh_host(path: Path, **kwargs) -> Deployment:
    """A host with no RavensPort instances or networks, which has never
    rendered."""
    host = Deployment(path, **kwargs)
    host.update(lambda s: s.update(instances={}, networks=[]))
    host.remove_render()
    return host


def test_an_exported_set_restores_on_another_host(tmp_path: Path):
    source = Deployment(tmp_path / "source")
    source.prepare_set("week-1")
    file = _export(source)

    target = _fresh_host(tmp_path / "target")
    result = target.run("restore.sh", str(file))
    assert result.returncode == 0, result.stderr

    # The networks come from the file's rendered commands, and the instance
    # commands after them do not run. Nothing is rendered on this host.
    assert sorted(target.state["networks"]) == sorted(NETWORKS)
    assert not target.ran("incus init")
    assert not target.ran("make")
    assert not (target.build / "manifest.json").exists()
    imports = [
        i for i, line in enumerate(target.log) if line.startswith("incus import ")
    ]
    assert len(imports) == len(NODES)
    assert target.index("incus network create vnet-ot") < min(imports)

    set_dir = target.snapshots / "week-1"
    assert (set_dir / "checkpoint.json").read_bytes() == (
        source.snapshots / "week-1" / "checkpoint.json"
    ).read_bytes()
    assert (set_dir / "stopped-at").read_text() == "2025-03-01T12:04:00Z\n"
    for path in [f"rendered/{name}" for name in RENDERED_FILES] + ["inputs/world.yaml"]:
        assert (set_dir / path).read_bytes() == (
            source.snapshots / "week-1" / path
        ).read_bytes(), path
    assert target.deployed_set == "week-1"
    assert target.ran("sudo date -u -s 2025-03-01T12:04:05Z")
    for name, inst in target.state["instances"].items():
        assert inst["snapshots"] == ["week-1"], name
        assert inst["status"] == "Running", name
    assert len((set_dir / "runs.log").read_text().splitlines()) == 1


def test_an_import_warns_when_the_checkout_differs(tmp_path: Path):
    source = Deployment(tmp_path / "source")
    source.prepare_set("week-1")
    file = _export(source)
    target = _fresh_host(tmp_path / "target", head="b" * 40)
    result = target.run("import-snapshot.sh", str(file))
    assert result.returncode == 0, result.stderr
    assert f"WARNING: {file} was produced from commit {HEAD}" in result.stderr


def test_an_import_refuses_a_different_set_under_the_same_label(tmp_path: Path):
    source = Deployment(tmp_path / "source")
    source.prepare_set("week-1")
    file = _export(source)
    target = _fresh_host(tmp_path / "target")
    (target.snapshots / "week-1").mkdir(parents=True)
    (target.snapshots / "week-1" / "checkpoint.json").write_text(
        _checkpoint(timedelta(days=1))
    )
    result = target.run("import-snapshot.sh", str(file))
    _assert_failed(result, "holds a different set with the label 'week-1'")
    assert not target.changes()


def test_an_import_refuses_an_instance_on_a_network_the_topology_does_not_create(
    tmp_path: Path,
):
    source = Deployment(tmp_path / "source")
    source.prepare_set("week-1")
    file = _export(source)
    target = _fresh_host(tmp_path / "target")
    target.update(lambda s: s.update(import_network="vnet-removed"))
    result = target.run("import-snapshot.sh", str(file))
    _assert_failed(
        result,
        "attached to the network vnet-removed, which this topology does not create",
    )
    assert not target.ran("incus snapshot create")


# ── deployments of the competition profile (§10) ──────────────────────────────

COMPETITION_SIM_IMAGE = "ravensport/sim-competition:latest"


def _render_as_competition(directory: Path) -> None:
    """Record in the manifest in `directory` that sim-host is created from the
    competition image, as a render with the competition's additional override
    file records it."""
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["images"] = {"sim-host": COMPETITION_SIM_IMAGE}
    (directory / "manifest.json").write_text(json.dumps(manifest))


def test_a_deployment_of_the_competition_profile_takes_no_set(deployment: Deployment):
    _render_as_competition(deployment.build)
    before = deployment.state
    result = deployment.run("snapshot.sh", "week-1")
    _assert_failed(result, "competition profile takes no snapshot set")
    assert not (deployment.snapshots / "week-1").exists()
    assert deployment.changes() == []
    assert deployment.state == before


def test_a_set_taken_from_a_deployment_of_the_competition_profile_is_not_restored(
    deployment: Deployment,
):
    set_dir = deployment.prepare_set("week-1")
    _render_as_competition(set_dir / "rendered")
    before = deployment.state
    result = deployment.run("restore.sh", "week-1")
    _assert_failed(result, "competition profile")
    assert deployment.changes() == []
    assert not deployment.clock_held
    assert deployment.deployed_set is None
    assert deployment.state == before


def test_a_snapshot_file_from_a_deployment_of_the_competition_profile_is_not_imported(
    deployment: Deployment, tmp_path: Path
):
    rendered = tmp_path / "file-rendered"
    rendered.mkdir()
    deployment.write_manifest(rendered, NODES)
    _render_as_competition(rendered)
    snapshot_file = tmp_path / "ravensport-snapshot-week-1-ci-minimal.tar"
    with tarfile.open(snapshot_file, "w") as archive:
        archive.add(rendered / "manifest.json", arcname="rendered/manifest.json")
    before = deployment.state
    result = deployment.run("restore.sh", str(snapshot_file))
    _assert_failed(result, "competition profile")
    assert deployment.changes() == []
    assert not deployment.clock_held
    assert deployment.deployed_set is None
    assert deployment.state == before


def test_the_competition_image_is_the_one_the_competition_override_names():
    """The scripts know a deployment of the competition profile by the image
    that the competition's additional override file gives sim-host."""
    lib = (ROOT / "scripts" / "snapshot-lib.sh").read_text()
    match = re.search(r"^COMPETITION_SIM_IMAGE=(\S+)$", lib, re.MULTILINE)
    assert match, "COMPETITION_SIM_IMAGE not found"
    assert match.group(1) == COMPETITION_SIM_IMAGE
    override = (ROOT.parent / "competition" / "deploy" / "override.yaml").read_text()
    assert re.search(
        rf"^  sim-host:\n(?:    #.*\n)*    image: {re.escape(COMPETITION_SIM_IMAGE)}$",
        override,
        re.MULTILINE,
    ), "the competition's override gives sim-host another image"
    build = (ROOT / "scripts" / "build-sim-image.sh").read_text()
    assert f'IMAGE_ALIAS="{COMPETITION_SIM_IMAGE}"' in build


# ── the render a set records (§10.2) ──────────────────────────────────────────


def test_a_render_records_its_inputs():
    """A set copies the inputs build/ was rendered from."""
    subprocess.run(
        [sys.executable, "scripts/render.py", "--profile", "ci-minimal"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    inputs = ROOT / "build" / "inputs"
    for source, copy in [
        (ROOT / "world.yaml", inputs / "world.yaml"),
        (ROOT / "topology.yaml", inputs / "topology.yaml"),
        (
            ROOT / "overrides" / "ci-minimal.yaml",
            inputs / "overrides" / "ci-minimal.yaml",
        ),
    ]:
        assert copy.read_bytes() == source.read_bytes(), copy


# ── agreement with the sim ─────────────────────────────────────────────────────


def _rust_str(source: str, name: str) -> str:
    match = re.search(rf'const {name}: &str = "([^"]+)";', source)
    assert match, f"{name} not found"
    return match.group(1)


def test_the_start_up_input_and_the_checkpoint_share_no_path():
    """A deployment of the competition profile attaches the start-up input, and a
    restore the checkpoint's mount directory, at paths that share nothing
    (deployment.md §10)."""
    main = (SIM_SRC / "main.rs").read_text()
    entry = (ROOT.parent / "competition" / "sim" / "src" / "input.rs").read_text()
    deploy_script = (ROOT / "scripts" / "deploy.sh").read_text()
    restore = (ROOT / "scripts" / "restore.sh").read_text()
    build = (ROOT / "scripts" / "build-sim-image.sh").read_text()

    state_dir = Path(_rust_str(main, "STATE_DIR"))
    startup = Path(_rust_str(entry, "PATH"))
    assert startup.name == "startup.json"
    startup_dir = startup.parent
    assert f"path={startup_dir} " in deploy_script
    assert f"path={state_dir} " in restore
    assert f"path={state_dir} " not in deploy_script
    assert f"/rootfs{startup_dir}" in build, (
        "the competition image lacks the mount point"
    )
    assert state_dir != startup_dir
    assert state_dir not in startup_dir.parents
    assert startup_dir not in state_dir.parents


def test_the_scripts_use_the_sim_paths_and_limits():
    """The paths, file names and start gap the scripts use are those the sim
    reads and writes (sim.md §9)."""
    main = (SIM_SRC / "main.rs").read_text()
    checkpoint = (SIM_SRC / "checkpoint.rs").read_text()
    lib = (ROOT / "scripts" / "snapshot-lib.sh").read_text()
    snapshot = (ROOT / "scripts" / "snapshot.sh").read_text()
    restore = (ROOT / "scripts" / "restore.sh").read_text()

    run_dir = _rust_str(main, "RUN_DIR")
    state_dir = _rust_str(main, "STATE_DIR")
    checkpoint_file = _rust_str(checkpoint, "CHECKPOINT_FILE")
    assert f"{run_dir}/{checkpoint_file}" in snapshot
    assert f"{run_dir}/{_rust_str(checkpoint, 'LOADED_MARKER')}" in restore
    assert f"path={state_dir}" in restore
    assert f'"$MOUNT_DIR/{checkpoint_file}"' in restore
    assert f'"$MOUNT_DIR/{_rust_str(checkpoint, "RESEED_FILE")}"' in restore
    assert "pub tug_arrival_seed: Option<u64>" in checkpoint

    # The fuel supplier's next poll, which make snapshot reads (§10.2): a
    # field of the checkpoint's top-level supplier state.
    supplier = (SIM_SRC / "actors" / "fuel_supplier" / "mod.rs").read_text()
    assert re.search(
        r"pub struct Checkpoint \{[^}]*pub fuel_supplier: FuelSupplierState,",
        checkpoint,
    )
    assert re.search(
        r"pub struct FuelSupplierState \{[^}]*pub next_poll_at: PrimitiveDateTime,",
        supplier,
    )
    assert ".fuel_supplier.next_poll_at" in snapshot

    gap = re.search(
        r"const MAX_START_GAP: Duration = Duration::from_secs\((\d+) \* 60\);", main
    )
    assert gap, "MAX_START_GAP not found"
    assert f"MAX_START_GAP_S={int(gap.group(1)) * 60}" in lib

"""
A stub `incus` for the snapshot-set script tests (test_snapshot_scripts.py).

It keeps the instances, their status, snapshots and devices, and the networks in
a JSON state file named by $STUB_STATE, and appends every command it is given to
the log named by $STUB_LOG, as "incus <arguments>", so that a test can assert
both the end state and the order of the commands. Commands inside sim-host
answer from the state's "sim" entry: whether the sim service stops cleanly,
whether it wrote a checkpoint, whether the checkpoint can be copied out,
whether it accepts the checkpoint mounted for it, whether it stops soon after
starting, and the end of its log. An instance can be set to fail a clean stop
or the creation of a snapshot. Every start updates the
instance's last_used_at, as Incus does.

Only the commands the snapshot-set scripts give are recognised; any other is an
error, so that a script that gains a command fails its tests until the stub
models it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

STATE = Path(os.environ["STUB_STATE"])
LOG = Path(os.environ["STUB_LOG"])


def fail(message: str, code: int = 1):
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(code)


def instance(state: dict, name: str) -> dict:
    if name not in state["instances"]:
        fail(f"Instance not found: {name}")
    return state["instances"][name]


def query(state: dict, path: str):
    if state.get("unreachable"):
        fail('Get "http://unix.socket/1.0": dial unix: connect: connection refused')
    parts = path.strip("/").split("/")
    if parts == ["1.0"]:
        return {"environment": {"server_version": "6.0.0"}}
    if parts == ["1.0", "storage-pools", "default", "resources"]:
        return {"space": {"total": state["pool_total"], "used": 0}}
    if parts[:2] == ["1.0", "networks"] and len(parts) == 3:
        if parts[2] not in state["networks"]:
            fail("Network not found")
        return {"name": parts[2]}
    if parts == ["1.0", "instances"]:
        return [f"/1.0/instances/{name}" for name in state["instances"]]
    if parts[:2] == ["1.0", "instances"]:
        inst = instance(state, parts[2])
        if len(parts) == 3:
            return {
                "status": inst["status"],
                "expanded_devices": inst["devices"],
                "last_used_at": inst.get("last_used_at", "2025-03-01T08:00:00Z"),
            }
        if parts[3] == "snapshots" and len(parts) == 4:
            return [f"/{path.strip('/')}/{label}" for label in inst["snapshots"]]
        if parts[3] == "snapshots" and len(parts) == 5:
            if parts[4] not in inst["snapshots"]:
                fail("Snapshot not found")
            return {"name": parts[4], "config": inst.get("config", {})}
    fail(f"the stub does not model the query {path}")


def exec_in(state: dict, name: str, command: list[str]) -> int:
    inst = instance(state, name)
    if inst["status"] != "Running":
        fail(f"Instance is not running: {name}")
    sim = state["sim"]
    match command:
        case ["rc-service", "sim", "stop"]:
            # "started" is a stop that failed; "crashed" is a sim that died
            # before it came to rest, which OpenRC still reports as stopping.
            sim["service"] = sim["stops_as"]
            return 1 if sim["stops_as"] == "started" else 0
        case ["rc-service", "sim", "status"]:
            print(f" * status: {sim['service']}")
            return 0 if sim["service"] == "started" else 3
        case ["test", "-f", "/run/sim/checkpoint.json"]:
            return 0 if sim["writes_checkpoint"] and sim["service"] == "stopped" else 1
        case ["test", "-e", "/run/sim/checkpoint-loaded"]:
            mounted = inst["devices"].get("sim-state", {})
            loaded = (
                sim["accepts_checkpoint"]
                and mounted.get("path") == "/var/lib/sim/state"
                and mounted.get("readonly") == "true"
                and Path(
                    mounted.get("source", "/nonexistent"), "checkpoint.json"
                ).is_file()
            )
            return 0 if loaded else 1
        case ["tail", *_]:
            print(sim.get("log_tail", "sim: the checkpoint was refused"))
            return 0
        case ["curl", *args] if "https://127.0.0.1/api/v1/health" in args:
            print(
                '{"status":"ok"}'
                if state["instances"]["fms-01"]["status"] == "Running"
                else ""
            )
            return 0
        case ["curl", *args] if "https://10.40.0.12/health" in args:
            print(
                '{"status":"ok"}'
                if state["instances"]["hmi-01"]["status"] == "Running"
                else ""
            )
            return 0
        case ["timeout", _, "openssl", "s_client", *args] if "corp-it:636" in args:
            # The directory probe's TLS handshake with slapd on corp-it, which
            # the stub does not model as an instance: answered from fms-01,
            # from which the probe runs.
            return 0 if state["instances"]["fms-01"]["status"] == "Running" else 1
    fail(f"the stub does not model the command {command} in {name}")


def main(args: list[str]) -> int:
    with LOG.open("a") as log:
        log.write("incus " + " ".join(args) + "\n")
    state = json.loads(STATE.read_text())
    code = 0
    match args:
        case ["query", path]:
            print(json.dumps(query(state, path)))
        case ["exec", name, "--", *command]:
            code = exec_in(state, name, command)
        case ["file", "pull", source, destination]:
            if (
                source != "sim-host/run/sim/checkpoint.json"
                or state["sim"]["service"] != "stopped"
            ):
                fail(f"the stub does not model pulling {source}")
            if state["sim"].get("pull_fails"):
                fail("websocket: close 1006 (abnormal closure): unexpected EOF")
            Path(destination).write_text(state["sim"]["checkpoint"])
        case (
            ["stop", "--force", name] | ["stop", name] | ["stop", "--timeout", _, name]
        ):
            inst = instance(state, name)
            if inst["status"] != "Running":
                fail("The instance is already stopped")
            if inst.get("hangs_on_stop") and args[1] != "--force":
                fail(
                    'Failed shutting down instance, status is "Running": context deadline exceeded'
                )
            inst["status"] = "Stopped"
        case ["start", name]:
            inst = instance(state, name)
            if inst["status"] == "Running":
                fail("The instance is already running")
            inst["status"] = "Running"
            # As Incus does, every start updates last_used_at.
            state["starts"] = state.get("starts", 0) + 1
            inst["last_used_at"] = f"2025-03-02T00:00:{state['starts']:02d}Z"
            if name == "sim-host":
                # A sim that stops soon after starting has still marked the
                # checkpoint it accepted (/run/sim/checkpoint-loaded).
                stops = state["sim"].get("stops_after_start", False)
                state["sim"]["service"] = "crashed" if stops else "started"
        case ["snapshot", "create", name, label]:
            inst = instance(state, name)
            if label in inst["snapshots"]:
                fail("Snapshot already exists")
            if inst.get("snapshot_fails"):
                fail("Create instance snapshot: No space left on device")
            inst["snapshots"].append(label)
        case ["snapshot", "restore", name, label]:
            if label not in instance(state, name)["snapshots"]:
                fail("Snapshot not found")
        case ["config", "device", "add", name, device, kind, *settings]:
            inst = instance(state, name)
            if device in inst["devices"]:
                fail("The device already exists")
            inst["devices"][device] = {"type": kind} | dict(
                s.split("=", 1) for s in settings
            )
        case ["config", "device", "remove", name, device]:
            inst = instance(state, name)
            if device not in inst["devices"]:
                fail("Device doesn't exist")
            del inst["devices"][device]
        case ["copy", source, target]:
            name, _, label = source.partition("/")
            original = instance(state, name)
            if label not in original["snapshots"]:
                fail("Snapshot not found")
            # As Incus does, the copy is given new MAC addresses.
            state["instances"][target] = {
                **original,
                "status": "Stopped",
                "snapshots": [],
                "copied_from": name,
                "config": {
                    key: f"00:16:3e:00:00:{len(state['instances']):02x}"
                    for key in original.get("config", {})
                },
            }
        case ["config", "set", name, *settings]:
            instance(state, name).setdefault("config", {}).update(
                s.split("=", 1) for s in settings
            )
        case ["export", name, path, "--instance-only", "--compression", "zstd"]:
            inst = instance(state, name)
            subprocess.run(
                ["zstd", "-q", "-o", path], input=inst["export"].encode(), check=True
            )
            state.setdefault("exported_config", {})[name] = inst.get("config", {})
        case ["delete", "--force", name] | ["delete", name]:
            instance(state, name)
            del state["instances"][name]
        case ["import", path, name]:
            if name in state["instances"]:
                fail("Instance already exists")
            Path(path).read_bytes()
            state["instances"][name] = {
                "status": "Stopped",
                "snapshots": [],
                "devices": {
                    "eth0": {"type": "nic", "network": state["import_network"]}
                },
                "export": "",
            }
        case ["network", "show", name]:
            if name not in state["networks"]:
                fail("Network not found")
        case ["network", "create", name, *_]:
            state["networks"].append(name)
        case _:
            fail(f"the stub does not model incus {' '.join(args)}")
    STATE.write_text(json.dumps(state))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

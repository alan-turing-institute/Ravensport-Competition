"""Fixtures of the living port's suite (episodes-tests.md §0.6, what waits: a
living port and its forks in the integration suite).

The suite creates its living ports with the living port's manager,
competition.living. test_01_track_a_in_forks.py creates one living port in
training mode for each training volume that its cases need, and forks each at
once through the manager, so that no case waits for the drawn fork schedule.
test_02_same_seed.py creates two living ports in evaluation mode with one
living port seed, and lets each fork on its drawn schedule, since the fork
waits are among the draws that it compares.

The suite needs a host on which the manager runs, with QEMU on Linux or the
hypervisor of macOS, and a base disk, qcow2 or raw, frozen with the
competition images of the tree under test. Each input comes from an option or
from an environment variable. Without a base disk, its network card's address
or a scratch root, every test of the suite is skipped, so the other suites
are unaffected.

    --living-base-disk  RAVENSPORT_LIVING_BASE_DISK  the base disk, never written
    --living-mac        RAVENSPORT_LIVING_MAC        the base's network card's address
    --living-root       RAVENSPORT_LIVING_TEST_ROOT  the scratch root; each case's
                                                     manager keeps its living port
                                                     in ROOT/CASE/NAME
    --living-tree       RAVENSPORT_LIVING_TREE       the tree to deploy (default: this
                                                     repository)
    --living-user       RAVENSPORT_LIVING_USER       the guest's user (default: $USER)
    --living-key        RAVENSPORT_LIVING_KEY        the guest's SSH key (default:
                                                     Lima's key)
    --living-affinity   RAVENSPORT_LIVING_AFFINITY   host threads for the VMs, such as
                                                     8-15; none on macOS, whose
                                                     hypervisor does not pin a VM
    --living-parallel   RAVENSPORT_LIVING_PARALLEL   how many living ports at once
                                                     (default 1)
    --living-ssh-port   RAVENSPORT_LIVING_SSH_PORT   the first of the run's SSH ports;
                                                     each living port takes the next
                                                     10, its own and its forks',
                                                     test_02's from 100 above it
                                                     (default 42300)
    --living-run        RAVENSPORT_LIVING_RUN        the run's prefix of the living
                                                     ports' names (default: r and 6
                                                     random hexadecimal digits)
    --living-keep       RAVENSPORT_LIVING_KEEP       keep the living ports after the
                                                     module, when set to 1
    --living-cpus       RAVENSPORT_LIVING_CPUS       the VMs' processors (default 4)
    --living-memory-mib RAVENSPORT_LIVING_MEMORY_MIB the VMs' memory (default 8192)
    --living-cidata     RAVENSPORT_LIVING_CIDATA     a cloud-init image that the base
                                                     expects, if any
    --living-firmware   RAVENSPORT_LIVING_FIRMWARE   the VMs' firmware, if not found

The living ports are stopped and deleted after the module, unless a test of
the suite failed or the keep input is set. Before any living port is created,
the suite checks that each name fits the manager's rule and that each fork's
socket path fits the length that QEMU and the manager accept, and names the
input to shorten when one does not.

Run with: make -C tests/integration test-living
"""

from __future__ import annotations

import logging
import os
import platform
import re
import secrets
import socket
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

from competition.living import hypervisor as hypervisors
from competition.living import port as living_port
from competition.living.port import Base, Manager
from competition.living.qmp import MAX_SOCKET_PATH
from competition.living.ssh import Remote, Ssh
from competition.living.vm import Engine

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[3]

# Each input: its option, its environment variable, and its default.
INPUTS = {
    "base_disk": ("--living-base-disk", "RAVENSPORT_LIVING_BASE_DISK", None),
    "mac": ("--living-mac", "RAVENSPORT_LIVING_MAC", None),
    "root": ("--living-root", "RAVENSPORT_LIVING_TEST_ROOT", None),
    "tree": ("--living-tree", "RAVENSPORT_LIVING_TREE", str(REPO)),
    "user": ("--living-user", "RAVENSPORT_LIVING_USER", os.environ.get("USER", "")),
    "key": (
        "--living-key",
        "RAVENSPORT_LIVING_KEY",
        str(Path(os.environ.get("LIMA_HOME", "~/.lima")) / "_config" / "user"),
    ),
    "affinity": ("--living-affinity", "RAVENSPORT_LIVING_AFFINITY", None),
    "parallel": ("--living-parallel", "RAVENSPORT_LIVING_PARALLEL", "1"),
    "ssh_port": ("--living-ssh-port", "RAVENSPORT_LIVING_SSH_PORT", "42300"),
    "run": ("--living-run", "RAVENSPORT_LIVING_RUN", None),
    "keep": ("--living-keep", "RAVENSPORT_LIVING_KEEP", None),
    "cpus": ("--living-cpus", "RAVENSPORT_LIVING_CPUS", "4"),
    "memory_mib": ("--living-memory-mib", "RAVENSPORT_LIVING_MEMORY_MIB", "8192"),
    "cidata": ("--living-cidata", "RAVENSPORT_LIVING_CIDATA", None),
    "firmware": ("--living-firmware", "RAVENSPORT_LIVING_FIRMWARE", None),
}
REQUIRED = ("base_disk", "mac", "root")
# Whether a test of the suite failed, so that its living ports are kept.
FAILED = pytest.StashKey[bool]()
# The suite imports the episode runner in-process, and the episode runner reads
# TOML with the standard library's tomllib, which Python 3.11 added.
MINIMUM_PYTHON = (3, 11)


def pytest_configure(config: pytest.Config) -> None:
    if sys.version_info < MINIMUM_PYTHON:
        needed = ".".join(str(n) for n in MINIMUM_PYTHON)
        raise pytest.UsageError(
            f"the living port's suite needs Python {needed} or later, which the "
            f"episode runner's tomllib needs; {sys.executable} is Python "
            f"{platform.python_version()}. Remove tests/integration/.venv-*, "
            f"then run make -C tests/integration PYTHON=<Python {needed} or "
            "later> test-living"
        )


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("living port", "the living port's suite")
    for name, (option, variable, _) in INPUTS.items():
        group.addoption(option, dest=f"living_{name}", help=f"or ${variable}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    if outcome.get_result().failed:
        item.session.stash[FAILED] = True


def a_test_failed(session: pytest.Session) -> bool:
    return session.stash.get(FAILED, False)


@dataclass(frozen=True)
class Inputs:
    """The host's inputs to the suite."""

    base_disk: Path
    mac: str
    root: Path
    tree: Path
    user: str
    key: Path
    affinity: str | None
    parallel: int
    ssh_port: int
    run: str
    keep: bool
    cpus: int
    memory_mib: int
    cidata: Path | None
    firmware: Path | None


def given(config: pytest.Config, name: str) -> str | None:
    """An input from its option, or else its environment variable, or else
    its default. The option exists only when this directory's conftest is
    among the initial ones, as when pytest is given this directory."""
    option, variable, default = INPUTS[name]
    value = config.getoption(f"living_{name}", default=None)
    return value or os.environ.get(variable) or default


def run_prefix() -> str:
    """r and 6 random hexadecimal digits, such as r5eda3e: short enough for
    every case's name to fit the manager's rule of at most 32 characters."""
    return f"r{secrets.token_hex(3)}"


def check_the_ports_fit(inputs: Inputs, names: dict[str, str]) -> None:
    """Before any living port is created, fail with the input to shorten
    when a living port's name breaks the manager's rule, or when the QMP
    socket of a fork, the longest socket path that the suite makes, is longer
    than QEMU and the manager accept (MAX_SOCKET_PATH, qmp.py). `names` maps
    each case to its living port's name. Each case's manager keeps its root
    in ROOT/CASE, the living port in ROOT/CASE/NAME, fork k in
    ROOT/CASE/NAME/copies/fork-k and each VM's socket in its directory as
    qmp.sock (port.py, restore_fork; vm.py, Machine.qmp_socket)."""
    for name in names.values():
        if not living_port._NAME.fullmatch(name):
            pytest.fail(
                f"the living port's name {name} breaks the manager's rule "
                f"{living_port._NAME.pattern}: shorten --living-run or "
                f"${INPUTS['run'][1]}"
            )
    socket_path = max(
        (
            inputs.root / case / name / "copies" / "fork-1" / "qmp.sock"
            for case, name in names.items()
        ),
        key=lambda path: len(str(path).encode()),
    )
    size = len(str(socket_path).encode())
    if size > MAX_SOCKET_PATH:
        pytest.fail(
            f"the fork's QMP socket {socket_path} takes {size} bytes, more "
            f"than the {MAX_SOCKET_PATH} that QEMU and the manager accept: "
            f"shorten --living-root or ${INPUTS['root'][1]}, or "
            f"--living-run or ${INPUTS['run'][1]}"
        )


def ssh_ports(first: int, count: int) -> Callable[[], int]:
    """A source of free Transmission Control Protocol (TCP) ports on
    127.0.0.1 from first, up to count of them, for one living port's forks."""
    ports = iter(range(first, first + count))
    lock = threading.Lock()

    def take() -> int:
        with lock:
            for candidate in ports:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    try:
                        sock.bind(("127.0.0.1", candidate))
                    except OSError:
                        continue
                return candidate
        raise RuntimeError(f"no free SSH port left from {first}")

    return take


@dataclass
class Port:
    """One case's living port and its manager."""

    case: str
    name: str
    manager: Manager
    inputs: Inputs
    # The living port's own SSH port; its forks take the next 9.
    ssh_port: int
    created: bool = False

    def remote(self, ssh_port: int) -> Remote:
        return Remote(Ssh(ssh_port, self.inputs.user, self.inputs.key))

    def base(self) -> Base:
        inputs = self.inputs
        return Base(
            disk=inputs.base_disk,
            mac=inputs.mac,
            user=inputs.user,
            key=inputs.key,
            cpus=inputs.cpus,
            memory_mib=inputs.memory_mib,
            cidata=inputs.cidata,
        )


def living_ports(inputs: Inputs, names: dict[str, str], first: int) -> dict[str, Port]:
    """Each case's living port, from `names`, which maps each case to its
    living port's name, once the names and the socket paths fit. Each living
    port has a manager, a manager's root and SSH ports of its own, a block of
    10 from `first`: the living port lies in ROOT/CASE/NAME. A manager writes
    a probe of its root's clones there, named by the process alone, so
    managers of this one process never share a root."""
    check_the_ports_fit(inputs, names)
    ports = {}
    for n, (case, name) in enumerate(names.items()):
        (inputs.root / case).mkdir(parents=True, exist_ok=True)
        ports[case] = Port(
            case=case,
            name=name,
            manager=Manager(
                inputs.root / case,
                Engine(hypervisors.for_host(inputs.firmware)),
                port=ssh_ports(first + 10 * n + 1, 9),
            ),
            inputs=inputs,
            ssh_port=first + 10 * n,
        )
    return ports


def delete_ports(ports: Iterable[Port], session: pytest.Session) -> None:
    """Stop and delete each living port that the module created, unless a
    test failed or the keep input is set."""
    ports = list(ports)
    if not ports:
        return
    if ports[0].inputs.keep or a_test_failed(session):
        log.warning(
            "keeping the living ports %s below %s",
            ", ".join(port.name for port in ports),
            ports[0].inputs.root,
        )
        return
    for port in ports:
        if port.created and port.manager.port_dir(port.name).exists():
            try:
                port.manager.stop(port.name, delete=True)
            except Exception as error:  # noqa: BLE001 - reported, the others go on
                log.error("%s was not deleted: %s", port.name, error)


@pytest.fixture(scope="session")
def inputs(pytestconfig: pytest.Config) -> Inputs:
    missing = [
        f"{INPUTS[name][0]} or ${INPUTS[name][1]}"
        for name in REQUIRED
        if not given(pytestconfig, name)
    ]
    if missing:
        pytest.skip(f"the living port's suite needs {', '.join(missing)}")

    def path(name: str) -> Path | None:
        value = given(pytestconfig, name)
        return Path(value).expanduser().resolve() if value else None

    run = given(pytestconfig, "run") or run_prefix()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", run):
        pytest.fail(f"the run's prefix {run!r} holds other than a-z, 0-9 and -")
    parallel = int(given(pytestconfig, "parallel"))
    if parallel < 1:
        pytest.fail("--living-parallel takes a positive number of living ports")
    return Inputs(
        base_disk=path("base_disk"),
        mac=given(pytestconfig, "mac"),
        root=path("root"),
        tree=path("tree"),
        user=given(pytestconfig, "user"),
        key=path("key"),
        affinity=given(pytestconfig, "affinity"),
        parallel=parallel,
        ssh_port=int(given(pytestconfig, "ssh_port")),
        run=run,
        keep=(given(pytestconfig, "keep") or "").lower() in ("1", "yes", "true"),
        cpus=int(given(pytestconfig, "cpus")),
        memory_mib=int(given(pytestconfig, "memory_mib")),
        cidata=path("cidata"),
        firmware=path("firmware"),
    )

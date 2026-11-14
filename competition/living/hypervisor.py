"""The platform-specific parts of a living port's virtual machine (VM): the
emulator's binary, the accelerator, the machine type, the processor model, the
firmware, the pinning of the VM to host threads, the save's format, and the
host's available memory. The rest of the command line is the fixed device set
of vm.py, the same on every platform.

Three platforms are defined.

- Linux with the kernel's virtual machine support (KVM), tested on a
  Linux host: the Q35 machine type, the host's processor model, QEMU's own
  x86_64 firmware, pinning by taskset, and saves in QEMU's mapped random-access
  memory (RAM) format over 4 channels, which paused the port for about 5 s.
- macOS with Apple's Hypervisor framework (HVF), from the command line that a
  fork of the port tested on a Mac with an Apple processor: the `virt` machine type with high
  memory, the host's processor model, no default devices, Homebrew's aarch64
  firmware, no pinning, and saves as a stream with the bandwidth limit raised,
  which paused the port for 1.7 s where mapped RAM paused it for 13 s. The
  package has not yet run on the Mac.
- On an Intel Mac, whose guests are x86_64, HVF runs the Linux host's machine
  instead: the Q35 machine type, the host's processor model and QEMU's own
  x86_64 firmware, with no pinning and saves as a stream, as on a Mac with an
  Apple processor.
  HVF on Intel keeps the guest's time-stamp counter (TSC) counting while the
  VM is stopped and across a restore, so a guest on the TSC loses the held
  clock at every save. The guest's high precision event timer (HPET) counts
  QEMU's virtual clock, which stops with the VM and is saved with it. The
  machine keeps its HPET, and the guest's clock source is switched to it at
  the living port's creation (`guest_clocksource`).

Every save pauses the VM before the migration starts, which the Mac requires:
QEMU with HVF aborts when a save runs with the guest running.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# No practical limit on the migration's bandwidth: QEMU's default, 128 MiB/s,
# stretched the pause of an 8 GiB guest to a minute in the Linux and Mac tests.
MAX_BANDWIDTH = 100 * 1024**3


class HypervisorError(Exception):
    """The host cannot run the living port's VMs."""


@dataclass(frozen=True)
class Tuning:
    """The save's format, the same for a save and for the restore from it."""

    max_bandwidth: int = MAX_BANDWIDTH
    # QEMU's mapped RAM format places each page at a fixed offset in the state
    # file, written and read by several channels. Without it, the state file
    # is one stream.
    mapped_ram: bool = True
    multifd_channels: int = 4


MAPPED_RAM = Tuning()
STREAM = Tuning(mapped_ram=False)


@dataclass(frozen=True)
class Hypervisor:
    """What the command line of every VM takes from its platform."""

    name: str
    binary: str
    # Arguments that choose the machine type, the accelerator and the processor.
    machine: tuple[str, ...]
    # The read-only firmware image, the code half of the Extensible Firmware
    # Interface's (EFI's) flash, with no variable store.
    firmware: Path
    # Whether the platform pins a VM to host threads with taskset.
    pins: bool
    tuning: Tuning = field(default=MAPPED_RAM)
    # The clock source that the guest switches to when the living port is
    # created, where the guest's default does not stand still while the VM is
    # stopped; None keeps the guest's default.
    guest_clocksource: str | None = None

    def args(self) -> list[str]:
        """The platform's part of the command line."""
        return [
            *self.machine,
            "-drive",
            f"if=pflash,format=raw,readonly=on,file={self.firmware}",
        ]

    def pinned(self, argv: list[str], cpus: str | None) -> list[str]:
        """The command line, run on the host threads `cpus` (such as 0-15)
        where the platform pins VMs."""
        if not cpus:
            return argv
        if not self.pins:
            raise HypervisorError(f"{self.name} does not pin a VM to host threads")
        return ["taskset", "-c", cpus, *argv]

    def available_memory_bytes(self) -> int | None:
        """The memory the host can give a new VM, or None where unknown."""
        if self.name == LINUX_KVM:
            return linux_available_memory()
        if self.name == MACOS_HVF:
            return macos_available_memory()
        return None


LINUX_KVM = "linux-kvm"
MACOS_HVF = "macos-hvf"
# The firmware files that QEMU's own installation and the Linux distributions
# provide, in order of preference, relative to an installation prefix.
X86_64_FIRMWARE = (
    "share/qemu/edk2-x86_64-code.fd",
    "share/OVMF/OVMF_CODE_4M.fd",
    "share/OVMF/OVMF_CODE.fd",
)
AARCH64_FIRMWARE = ("share/qemu/edk2-aarch64-code.fd",)


def linux_kvm(
    firmware: Path | None = None, binary: str = "qemu-system-x86_64"
) -> Hypervisor:
    """QEMU on Linux, with KVM and the host's processor model, on the Q35
    machine type, as Lima's VMs of type qemu run."""
    return Hypervisor(
        name=LINUX_KVM,
        binary=binary,
        machine=("-machine", "q35,accel=kvm", "-cpu", "host"),
        firmware=firmware or find_firmware(binary, X86_64_FIRMWARE),
        pins=True,
        tuning=MAPPED_RAM,
    )


def macos_hvf(
    firmware: Path | None = None, binary: str = "qemu-system-aarch64"
) -> Hypervisor:
    """QEMU on macOS with HVF, as a fork of the port ran it on a Mac with an
    Apple processor."""
    return Hypervisor(
        name=MACOS_HVF,
        binary=binary,
        machine=(
            "-machine",
            "virt,accel=hvf,highmem=on",
            "-cpu",
            "host",
            "-nodefaults",
        ),
        firmware=firmware or find_firmware(binary, AARCH64_FIRMWARE),
        pins=False,
        tuning=STREAM,
    )


def macos_hvf_x86_64(
    firmware: Path | None = None, binary: str = "qemu-system-x86_64"
) -> Hypervisor:
    """QEMU on an Intel Mac with HVF: the Linux host's Q35 machine and x86_64
    firmware, with the Mac's stream format and no pinning. The name is the
    Mac's, so that the host's available memory comes from vm_stat. The
    machine keeps its HPET, and the guest's clock source is the HPET, whose
    count stops with the VM and resumes from the save in a fork; the guest's
    default, the TSC, counts on through every pause."""
    return Hypervisor(
        name=MACOS_HVF,
        binary=binary,
        machine=("-machine", "q35,accel=hvf,hpet=on", "-cpu", "host"),
        firmware=firmware or find_firmware(binary, X86_64_FIRMWARE),
        pins=False,
        tuning=STREAM,
        guest_clocksource="hpet",
    )


def for_host(firmware: Path | None = None) -> Hypervisor:
    """The hypervisor of the host this runs on."""
    if sys.platform.startswith("linux"):
        return linux_kvm(firmware)
    if sys.platform == "darwin":
        if platform.machine() == "x86_64":
            return macos_hvf_x86_64(firmware)
        return macos_hvf(firmware)
    raise HypervisorError(f"no hypervisor is defined for {sys.platform}")


def find_firmware(binary: str, candidates: tuple[str, ...]) -> Path:
    """The first firmware file found under the binary's installation prefix,
    then under /opt/homebrew, /usr/local and /usr."""
    found = shutil.which(binary)
    prefixes = [Path(found).resolve().parents[1]] if found else []
    prefixes += [Path("/opt/homebrew"), Path("/usr/local"), Path("/usr")]
    for prefix in prefixes:
        for candidate in candidates:
            if (prefix / candidate).is_file():
                return prefix / candidate
    raise HypervisorError(
        f"no firmware for {binary} under {', '.join(map(str, prefixes))}"
    )


def linux_available_memory(meminfo: Path = Path("/proc/meminfo")) -> int | None:
    """MemAvailable from /proc/meminfo, in bytes."""
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def macos_available_memory(vm_stat: str | None = None) -> int | None:
    """The free, inactive and speculative pages that vm_stat reports, in
    bytes."""
    if vm_stat is None:
        try:
            vm_stat = subprocess.run(
                ["vm_stat"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            ).stdout
        except OSError:
            return None
    size = re.search(r"page size of (\d+) bytes", vm_stat)
    pages = 0
    for kind in ("free", "inactive", "speculative"):
        found = re.search(rf"Pages {kind}:\s+(\d+)", vm_stat)
        if found is None:
            return None
        pages += int(found.group(1))
    return pages * int(size.group(1)) if size else None

"""The platform-specific parts of the command line.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from competition.living import hypervisor


def test_linux_runs_kvm_on_q35_with_the_hosts_processor() -> None:
    kvm = hypervisor.linux_kvm(firmware=Path("/fw/code.fd"))
    assert kvm.args() == [
        "-machine",
        "q35,accel=kvm",
        "-cpu",
        "host",
        "-drive",
        "if=pflash,format=raw,readonly=on,file=/fw/code.fd",
    ]
    assert kvm.pinned(["qemu"], "0-15") == ["taskset", "-c", "0-15", "qemu"]
    assert kvm.pinned(["qemu"], None) == ["qemu"]


def test_macos_runs_hvf_on_the_virt_machine_and_saves_as_a_stream() -> None:
    """The command line and the save's format of a fork of the port on a Mac
    with an Apple processor: no default devices, no pinning, and a stream with the bandwidth
    limit raised."""
    hvf = hypervisor.macos_hvf(firmware=Path("/fw/edk2-aarch64-code.fd"))
    assert hvf.binary == "qemu-system-aarch64"
    assert hvf.args() == [
        "-machine",
        "virt,accel=hvf,highmem=on",
        "-cpu",
        "host",
        "-nodefaults",
        "-drive",
        "if=pflash,format=raw,readonly=on,file=/fw/edk2-aarch64-code.fd",
    ]
    assert hvf.tuning == hypervisor.STREAM and not hvf.tuning.mapped_ram
    assert hypervisor.linux_kvm(firmware=Path("/fw")).tuning == hypervisor.MAPPED_RAM
    with pytest.raises(hypervisor.HypervisorError, match="does not pin"):
        hvf.pinned(["qemu"], "0-3")


def test_an_intel_mac_runs_hvf_on_q35_and_saves_as_a_stream() -> None:
    """An Intel Mac's guest is x86_64: the Linux host's machine and firmware,
    with HVF, the Mac's stream format and no pinning. It keeps the Mac's
    name, so that its available memory comes from vm_stat."""
    hvf = hypervisor.macos_hvf_x86_64(firmware=Path("/fw/edk2-x86_64-code.fd"))
    assert hvf.binary == "qemu-system-x86_64"
    assert hvf.name == hypervisor.MACOS_HVF
    assert hvf.args() == [
        "-machine",
        "q35,accel=hvf,hpet=on",
        "-cpu",
        "host",
        "-drive",
        "if=pflash,format=raw,readonly=on,file=/fw/edk2-x86_64-code.fd",
    ]
    assert hvf.tuning == hypervisor.STREAM
    with pytest.raises(hypervisor.HypervisorError, match="does not pin"):
        hvf.pinned(["qemu"], "0-3")


def test_only_an_intel_mac_switches_the_guests_clock_source() -> None:
    """HVF on Intel keeps the guest's TSC counting while the VM is stopped, so
    the guest's clock source there is the HPET, which stops with the VM. On
    Linux and on the Apple Silicon Mac the guest keeps its default."""
    firmware = Path("/fw/code.fd")
    assert hypervisor.macos_hvf_x86_64(firmware=firmware).guest_clocksource == "hpet"
    assert hypervisor.linux_kvm(firmware=firmware).guest_clocksource is None
    assert hypervisor.macos_hvf(firmware=firmware).guest_clocksource is None


@pytest.mark.parametrize(
    ("system", "machine", "binary", "accelerator"),
    [
        ("linux", "x86_64", "qemu-system-x86_64", "q35,accel=kvm"),
        ("darwin", "arm64", "qemu-system-aarch64", "virt,accel=hvf,highmem=on"),
        ("darwin", "x86_64", "qemu-system-x86_64", "q35,accel=hvf,hpet=on"),
    ],
)
def test_the_host_selects_its_platform(
    monkeypatch, system, machine, binary, accelerator
) -> None:
    monkeypatch.setattr(hypervisor.sys, "platform", system)
    monkeypatch.setattr(hypervisor.platform, "machine", lambda: machine)
    chosen = hypervisor.for_host(Path("/fw/code.fd"))
    assert chosen.binary == binary
    assert chosen.args()[:2] == ["-machine", accelerator]


def test_a_host_with_no_hypervisor_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(hypervisor.sys, "platform", "win32")
    with pytest.raises(hypervisor.HypervisorError, match="no hypervisor"):
        hypervisor.for_host(Path("/fw/code.fd"))


def test_the_macos_available_memory_comes_from_vm_stat() -> None:
    report = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               1000.\n"
        "Pages active:                             9000.\n"
        "Pages inactive:                           2000.\n"
        "Pages speculative:                         500.\n"
    )
    assert hypervisor.macos_available_memory(report) == 3500 * 16384
    assert hypervisor.macos_available_memory("no report") is None


def test_a_platform_that_does_not_pin_refuses_an_affinity() -> None:
    unpinned = hypervisor.Hypervisor("other", "qemu", (), Path("/fw"), pins=False)
    with pytest.raises(hypervisor.HypervisorError, match="does not pin"):
        unpinned.pinned(["qemu"], "0-3")


def test_the_firmware_is_found_under_the_binarys_prefix(tmp_path, monkeypatch) -> None:
    (tmp_path / "bin").mkdir()
    binary = tmp_path / "bin" / "qemu-system-x86_64"
    binary.write_text("")
    binary.chmod(0o755)
    (tmp_path / "share" / "qemu").mkdir(parents=True)
    (tmp_path / "share" / "qemu" / "edk2-x86_64-code.fd").write_text("")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    found = hypervisor.find_firmware("qemu-system-x86_64", hypervisor.X86_64_FIRMWARE)
    assert found == tmp_path.resolve() / "share" / "qemu" / "edk2-x86_64-code.fd"


def test_the_available_memory_is_read_from_meminfo(tmp_path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 65000000 kB\nMemAvailable: 61865984 kB\n")
    assert hypervisor.linux_available_memory(meminfo) == 61865984 * 1024
    assert hypervisor.linux_available_memory(tmp_path / "absent") is None

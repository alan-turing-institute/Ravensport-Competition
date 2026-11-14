"""The virtual machine (VM) engine (episodes.md §6.4, how a fork is made),
against fake QEMU processes on UNIX sockets.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
from living_fakes import QEMU_8_2

from competition.living.hypervisor import linux_kvm
from competition.living.vm import (
    DISK_ID,
    Engine,
    Machine,
    Tuning,
    VMError,
    command_line,
)

KVM = linux_kvm(firmware=Path("/usr/local/share/qemu/edk2-x86_64-code.fd"))
MAC = "52:55:55:a4:9c:41"


def machine(root: Path, name: str = "living-lp1", port: int = 41001) -> Machine:
    return Machine(
        name=name,
        disk=root / "disk-0.qcow2",
        rundir=root / "run",
        ssh_port=port,
        mac=MAC,
        affinity="0-15",
    )


def engine(host, **kwargs) -> Engine:
    # Through lambdas, so that a test may replace the host's popen later.
    return Engine(
        KVM,
        run=lambda argv, **kw: host.run(argv, **kw),
        popen=lambda argv, **kw: host.popen(argv, **kw),
        poll_s=0.001,
        **kwargs,
    )


def test_the_device_set_has_one_forward_on_the_loopback_and_no_host_share(
    tmp_path,
) -> None:
    argv = command_line(machine(tmp_path), KVM)
    assert argv[0] == "qemu-system-x86_64"
    assert argv[argv.index("-machine") + 1] == "q35,accel=kvm"
    assert argv[argv.index("-cpu") + 1] == "host"
    joined = " ".join(argv)
    assert "hostfwd=tcp:127.0.0.1:41001-:22" in joined
    assert f"virtio-net-pci,netdev=net0,mac={MAC}" in joined
    assert (
        f"file={tmp_path}/disk-0.qcow2,format=qcow2,if=none,discard=on,id={DISK_ID}"
        in joined
    )
    assert f"path={tmp_path}/run/qmp.sock,server=on,wait=off" in joined
    assert f"path={tmp_path}/run/serial.log" in joined
    assert argv[argv.index("-pidfile") + 1] == f"{tmp_path}/run/qemu.pid"
    assert (
        "-virtfs" not in argv and "virtio-9p" not in joined and "-incoming" not in argv
    )


def test_a_copy_changes_only_the_disk_sockets_port_pidfile_and_name(tmp_path) -> None:
    original = machine(tmp_path)
    copy = original.copy(
        name="living-lp1-fork-1",
        disk=tmp_path / "copies" / "fork-1" / "disk.qcow2",
        rundir=tmp_path / "copies" / "fork-1",
        ssh_port=41002,
    )
    before = command_line(original, KVM)
    after = command_line(copy, KVM, incoming=True)
    assert after[-2:] == ["-incoming", "defer"]
    after = after[:-2]
    assert len(before) == len(after)
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert changed == [
        ("living-lp1", "living-lp1-fork-1"),
        (f"file={tmp_path}/disk-0.qcow2,format=qcow2,if=none,discard=on,id=boot-disk",
         f"file={tmp_path}/copies/fork-1/disk.qcow2,format=qcow2,if=none,discard=on,id=boot-disk"),
        ("user,id=net0,net=192.168.5.0/24,dhcpstart=192.168.5.15,hostfwd=tcp:127.0.0.1:41001-:22",
         "user,id=net0,net=192.168.5.0/24,dhcpstart=192.168.5.15,hostfwd=tcp:127.0.0.1:41002-:22"),
        (f"file,id=serial0,path={tmp_path}/run/serial.log",
         f"file,id=serial0,path={tmp_path}/copies/fork-1/serial.log"),
        (f"socket,id=qmp0,path={tmp_path}/run/qmp.sock,server=on,wait=off",
         f"socket,id=qmp0,path={tmp_path}/copies/fork-1/qmp.sock,server=on,wait=off"),
        (f"{tmp_path}/run/qemu.pid", f"{tmp_path}/copies/fork-1/qemu.pid"),
    ]  # fmt: skip


def test_a_path_with_a_comma_is_refused(tmp_path) -> None:
    with pytest.raises(VMError, match="comma"):
        command_line(machine(tmp_path / "a,b"), KVM)


def test_an_overlay_is_created_on_its_backing_file_by_qemu_img(short_tmp, host) -> None:
    host.formats["/base/disk"] = "raw"
    engine(host).create_overlay(Path("/base/disk"), short_tmp / "lp1" / "disk-0.qcow2")
    assert host.runs[0] == ["qemu-img", "info", "-U", "--output=json", "/base/disk"]
    assert host.runs[1:] == [
        ["qemu-img", "create", "-q", "-f", "qcow2", "-b", "/base/disk", "-F", "raw",
         f"{short_tmp}/lp1/disk-0.qcow2"],
    ]  # fmt: skip
    with pytest.raises(VMError, match="exists"):
        engine(host).create_overlay(
            Path("/base/disk"), short_tmp / "lp1" / "disk-0.qcow2"
        )


def test_a_launch_is_pinned_and_returns_once_the_qmp_socket_exists(
    short_tmp, host
) -> None:
    vm = machine(short_tmp)
    pid = engine(host).launch(vm)
    assert pid == host.processes[0].pid
    assert host.launches[0][:3] == ["taskset", "-c", "0-15"]
    assert vm.qmp_socket.exists()
    assert (vm.rundir / "argv.json").is_file()


def test_a_launch_that_exits_at_once_reports_qemus_log(short_tmp, host) -> None:
    host.fail_launch = 1
    with pytest.raises(VMError, match="exited with 1: .*host forwarding rule"):
        engine(host).launch(machine(short_tmp))


def test_a_save_pauses_switches_the_disk_migrates_and_continues(
    short_tmp, host
) -> None:
    vm = machine(short_tmp)
    vms = engine(host)
    vms.launch(vm)
    qemu = host.servers["living-lp1"]
    saved = vms.save(
        vm, short_tmp / "disk-1.qcow2", short_tmp / "states" / "fork-1.mig"
    )
    assert qemu.names() == [
        "query-status",
        "query-block",
        "query-version",
        "query-migrate-capabilities",
        "migrate-set-capabilities",
        "migrate-set-parameters",
        "stop",
        "blockdev-snapshot-sync",
        "migrate",
        "query-migrate",
        "query-migrate",
        "cont",
    ]
    arguments = dict(qemu.commands)
    assert arguments["blockdev-snapshot-sync"] == {
        "device": "boot-disk",
        "snapshot-file": f"{short_tmp}/disk-1.qcow2",
        "format": "qcow2",
    }
    assert arguments["migrate"] == {"uri": f"file:{short_tmp}/states/fork-1.mig"}
    assert arguments["migrate-set-capabilities"]["capabilities"] == [
        {"capability": "mapped-ram", "state": True},
        {"capability": "multifd", "state": True},
    ]
    assert arguments["migrate-set-parameters"] == {
        "max-bandwidth": 100 << 30,
        "multifd-channels": 4,
    }
    assert qemu.status == "running"
    assert saved.frozen == short_tmp / "disk-0.qcow2"
    assert saved.overlay == short_tmp / "disk-1.qcow2"
    assert saved.state_bytes == 8192
    assert saved.pause_s >= saved.migrate_s >= 0
    assert saved.resumed_at >= saved.stopped_at
    assert saved.migration["status"] == "completed"
    assert vms.top(vm) == short_tmp / "disk-1.qcow2"


def test_a_save_without_mapped_ram_raises_only_the_bandwidth(short_tmp, host) -> None:
    vm = machine(short_tmp)
    vms = engine(host, tuning=Tuning(mapped_ram=False))
    vms.launch(vm)
    vms.save(vm, short_tmp / "disk-1.qcow2", short_tmp / "fork-1.mig")
    qemu = host.servers["living-lp1"]
    # A stream needs no capability, so none is set.
    assert "migrate-set-capabilities" not in qemu.names()
    assert dict(qemu.commands)["migrate-set-parameters"] == {"max-bandwidth": 100 << 30}


def test_a_failed_migration_still_continues_the_vm_and_removes_the_state(
    short_tmp, host
) -> None:
    vm = machine(short_tmp)
    vms = engine(host)
    vms.launch(vm)
    qemu = host.servers["living-lp1"]
    qemu.migration_end = "failed"
    state = short_tmp / "fork-1.mig"
    with pytest.raises(VMError, match="no space left"):
        vms.save(vm, short_tmp / "disk-1.qcow2", state)
    assert qemu.names()[-1] == "cont"
    assert qemu.status == "running"
    assert not state.exists()
    # The switch to the overlay happened, so the chain grew.
    assert (short_tmp / "disk-1.qcow2").exists()


def test_a_save_of_a_paused_vm_is_refused(short_tmp, host) -> None:
    vm = machine(short_tmp)
    vms = engine(host)
    vms.launch(vm)
    host.servers["living-lp1"].status = "paused"
    with pytest.raises(VMError, match="paused, not running"):
        vms.save(vm, short_tmp / "disk-1.qcow2", short_tmp / "fork-1.mig")
    assert "stop" not in host.servers["living-lp1"].names()


def test_a_restore_loads_the_state_into_a_copy_and_continues_it(
    short_tmp, host
) -> None:
    vm = machine(short_tmp)
    vms = engine(host)
    vms.launch(vm)
    saved = vms.save(vm, short_tmp / "disk-1.qcow2", short_tmp / "fork-1.mig")
    copy = vm.copy(
        name="living-lp1-fork-1",
        disk=short_tmp / "c1" / "disk.qcow2",
        rundir=short_tmp / "c1",
        ssh_port=41002,
    )
    restored = vms.restore(copy, saved.frozen, saved.state)
    assert host.creates()[-1][-1] == str(copy.disk)
    assert host.creates()[-1][host.creates()[-1].index("-b") + 1] == str(
        short_tmp / "disk-0.qcow2"
    )
    assert host.launches[-1][-2:] == ["-incoming", "defer"]
    qemu = host.servers["living-lp1-fork-1"]
    assert qemu.names()[:3] == [
        "migrate-set-capabilities",
        "migrate-set-parameters",
        "migrate-incoming",
    ]
    assert dict(qemu.commands)["migrate-incoming"] == {
        "uri": f"file:{short_tmp}/fork-1.mig",
        "exit-on-error": False,
    }
    assert qemu.names()[-2:] == ["cont", "query-status"]
    assert qemu.status == "running"
    assert restored.launch_to_running_s >= restored.load_s >= 0
    assert vms.status(copy) == "running"


def test_a_copy_whose_state_does_not_load_is_stopped(short_tmp, host) -> None:
    vm = machine(short_tmp)
    vms = engine(host)
    vms.launch(vm)
    saved = vms.save(vm, short_tmp / "disk-1.qcow2", short_tmp / "fork-1.mig")
    copy = vm.copy(
        name="copy",
        disk=short_tmp / "c1" / "disk.qcow2",
        rundir=short_tmp / "c1",
        ssh_port=41002,
    )
    real_popen = host.popen

    def failing(argv, **kwargs):
        process = real_popen(argv, **kwargs)
        host.servers["copy"].migration_end = "failed"
        return process

    host.popen = failing
    with pytest.raises(VMError, match="did not load: no space left"):
        vms.restore(copy, saved.frozen, saved.state)
    assert host.servers["copy"].names()[-1] == "quit"
    assert vms.status(copy) == "stopped"


def test_a_restore_without_its_state_file_is_refused(short_tmp, host) -> None:
    vm = machine(short_tmp)
    with pytest.raises(VMError, match="no state file"):
        engine(host).restore(vm, short_tmp / "disk-0.qcow2", short_tmp / "absent.mig")
    assert host.launches == []


def test_stop_quits_qemu_and_delete_removes_the_disk_and_run_directory(
    short_tmp, host
) -> None:
    vm = machine(short_tmp)
    vms = engine(host)
    vm.disk.write_bytes(b"QFI\xfb")
    vms.launch(vm)
    assert vms.status(vm) == "running"
    vms.delete(vm)
    assert host.servers["living-lp1"].names()[-1] == "quit"
    assert vms.status(vm) == "stopped"
    assert not vm.disk.exists() and not vm.rundir.exists()
    assert vms.stop(vm) is False


def test_a_machine_survives_its_record(tmp_path) -> None:
    vm = machine(tmp_path)
    assert Machine.from_dict(vm.to_dict()) == vm


def test_a_relative_path_is_refused(tmp_path) -> None:
    with pytest.raises(VMError, match="absolute"):
        command_line(machine(Path("relative")), KVM)


def test_a_cloud_init_image_is_attached_read_only(tmp_path) -> None:
    vm = Machine(
        name="lp",
        disk=tmp_path / "d.qcow2",
        rundir=tmp_path / "run",
        ssh_port=1,
        mac=MAC,
        cidata=tmp_path / "cidata.iso",
    )
    argv = command_line(vm, KVM)
    assert (
        f"id=cidata,if=none,format=raw,readonly=on,file={tmp_path}/cidata.iso" in argv
    )
    assert "virtio-blk-pci,drive=cidata" in argv
    assert Machine.from_dict(vm.to_dict()) == vm


def test_on_qemu_8_2_a_save_falls_back_to_the_stream_and_records_it(
    short_tmp, host
) -> None:
    """Ubuntu's QEMU 8.2 has no mapped RAM, and refuses the capability."""
    host.features = QEMU_8_2
    vm = machine(short_tmp)
    vms = engine(host)
    vms.launch(vm)
    saved = vms.save(vm, short_tmp / "disk-1.qcow2", short_tmp / "fork-1.mig")
    qemu = host.servers["living-lp1"]
    assert "migrate-set-capabilities" not in qemu.names()
    assert dict(qemu.commands)["migrate-set-parameters"] == {"max-bandwidth": 100 << 30}
    assert (saved.format, saved.qemu_version) == ("stream", "8.2.2")
    assert saved.to_dict()["format"] == "stream"


def test_on_qemu_8_2_a_restore_loads_the_stream_with_no_exit_on_error(
    short_tmp, host
) -> None:
    host.features = QEMU_8_2
    vm = machine(short_tmp)
    vms = engine(host)
    vms.launch(vm)
    saved = vms.save(vm, short_tmp / "disk-1.qcow2", short_tmp / "fork-1.mig")
    copy = vm.copy(
        name="copy",
        disk=short_tmp / "c1" / "disk.qcow2",
        rundir=short_tmp / "c1",
        ssh_port=2,
    )
    vms.restore(copy, saved.frozen, saved.state, fmt=saved.format)
    qemu = host.servers["copy"]
    assert dict(qemu.commands)["migrate-incoming"] == {"uri": f"file:{saved.state}"}
    assert "migrate-set-capabilities" not in qemu.names()
    assert qemu.status == "running"


def test_on_qemu_11_a_save_uses_mapped_ram_and_its_restore_exit_on_error(
    short_tmp, host
) -> None:
    vm = machine(short_tmp)
    vms = engine(host)
    vms.launch(vm)
    saved = vms.save(vm, short_tmp / "disk-1.qcow2", short_tmp / "fork-1.mig")
    assert (saved.format, saved.qemu_version) == ("mapped-ram", "11.1.2")
    copy = vm.copy(
        name="copy",
        disk=short_tmp / "c1" / "disk.qcow2",
        rundir=short_tmp / "c1",
        ssh_port=2,
    )
    vms.restore(copy, saved.frozen, saved.state, fmt=saved.format)
    qemu = host.servers["copy"]
    assert dict(qemu.commands)["migrate-incoming"]["exit-on-error"] is False
    assert dict(qemu.commands)["migrate-set-capabilities"]["capabilities"] == [
        {"capability": "mapped-ram", "state": True},
        {"capability": "multifd", "state": True},
    ]


def test_a_mapped_ram_state_on_a_qemu_without_it_is_refused(short_tmp, host) -> None:
    host.features = QEMU_8_2
    vm = machine(short_tmp)
    (short_tmp / "fork-1.mig").write_bytes(b"\0")
    copy = vm.copy(
        name="copy",
        disk=short_tmp / "c1" / "disk.qcow2",
        rundir=short_tmp / "c1",
        ssh_port=2,
    )
    with pytest.raises(VMError, match="mapped RAM, which QEMU 8.2.2 cannot load"):
        engine(host).restore(
            copy, short_tmp / "disk-0.qcow2", short_tmp / "fork-1.mig", fmt="mapped-ram"
        )
    assert host.servers["copy"].names()[-1] == "quit"


def test_the_features_are_read_once_for_every_vm(short_tmp, host) -> None:
    vm = machine(short_tmp)
    vms = engine(host)
    vms.launch(vm)
    vms.save(vm, short_tmp / "disk-1.qcow2", short_tmp / "fork-1.mig")
    vms.save(vm, short_tmp / "disk-2.qcow2", short_tmp / "fork-2.mig")
    assert host.servers["living-lp1"].names().count("query-version") == 1

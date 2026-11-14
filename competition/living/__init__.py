"""The living port's manager, on the physical host (episodes.md §6.3, the
living port; §6.4, how a fork is made).

The manager owns the Quick Emulator (QEMU) processes. It starts a living port
from a base disk, makes each fork by a save and a restore of the whole virtual
machine (VM), runs one episode in each fork over the Secure Shell (SSH),
collects the episode's outputs and deletes the fork. The episode runner runs
inside the VM, as `competition/episode_runner/` does today.

    qmp.py         a client of the QEMU Machine Protocol (QMP)
    hypervisor.py  the platform-specific parts of the command line
    vm.py          launch, save, restore, stop and delete one VM
    ssh.py         commands, files and clock readings over SSH
    port.py        the living port: create, fork, episode, loop, list, stop
    snapshots.py   the fork cadence and the snapshot schedule
    guest.py       the steps of creating a living port that run inside its VM

Every module but guest.py uses the standard library alone, so that the manager
runs on a host with no packages installed:

    python3 -m competition.living --help
"""

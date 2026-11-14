# Training on living ports

Training mode runs an entry's defender in forks of a living port, so that each episode starts from the port's accumulated history and the training volume passes from each episode to the next. This guide gives the commands and the preparation of the base disk from which a living port starts. The living port manager runs each living port and each fork as a virtual machine under the Quick Emulator (QEMU), with the kernel-based virtual machine (KVM) on Linux or Apple's Hypervisor framework on macOS. The specification [`episodes.md`](../specs/competition/episodes.md) is the authority for living ports and forks.


Training mode runs only in forks of a living port. The living port manager runs on the machine itself, outside the Incus host, from the repository's root. The command `create` starts a living port from a base disk, which the living port manager never writes, and deploys the port in it from the repository. `--training-volume` gives the first version of the training volume, and `--build` builds the images inside the living port before the deployment.

```sh
python3 -m competition.living create lp1 --base-disk DISK --mac MAC --tree . \
    --training-volume ~/volumes/learning --build
python3 -m competition.living loop lp1 --track A --type benign --forks 10
python3 -m competition.living loop lp1 --track A --type adversarial --forks 10
python3 -m competition.living list
python3 -m competition.living stop lp1 --delete
```

The command `loop` makes a fork about every 17 minutes on the living port's clock, and runs one episode in each fork, one at a time. Each episode leaves the next version of the training volume for the next fork. `python3 -m competition.living --help` lists every command.

The living port manager makes a fork only when the machine has the fork's memory and 1 GiB more available. Otherwise `loop` waits, and tries again every 30 seconds. `create --memory-mib` sets the memory of a living port and of its forks, 8192 by default. The smallest machine tested is a MacBook Pro with a 6-core Intel Core i7-9750H processor and 16 GiB of memory, with a web browser open. There, a living port and a fork of 6 GiB each ran a full episode. The check held the first fork back, with 5.3 GiB available, and the test made that fork without it.

## Prepare the base disk

The base disk is a copy of the Incus host's disk, taken after the competition images are built in it. The copy holds Ubuntu 24.04 on the 100 GiB disk that [`deploy/lima.yaml`](../deploy/lima.yaml) gives the Incus host, with Incus, the build tools and the images. A living port boots from the copy with the network card's address of the Incus host, because the guest's network configuration matches that address. The living port manager signs in to the guest as the user that Lima created there, who has the name of your user on the machine, with Lima's key, `~/.lima/_config/user`. The options `--user` and `--key` take these by default.

On Linux with KVM:

1. On the machine, install QEMU, its disk tool and its firmware, and give your user access to KVM. Sign in again so that the group membership applies.

   ```sh
   sudo apt install qemu-system-x86 qemu-utils ovmf
   sudo usermod -aG kvm "$USER"
   ```

2. Inside the Incus host, from the repository's root, build the competition images. Then stop the package timers, so that no unattended upgrade restarts Incus inside a living port.

   ```sh
   make -C deploy install
   make -C deploy stage-attacks
   make -C deploy build EXTRA_OVERRIDE=../competition/deploy/override.yaml
   sudo systemctl mask --now apt-daily.timer apt-daily-upgrade.timer
   ```

3. Inside the Incus host, read the network card's address. The value after `macaddress` is the address that `--mac` takes, and the address of `eth0` is the same.

   ```sh
   sudo grep macaddress /etc/netplan/50-cloud-init.yaml
   cat /sys/class/net/eth0/address
   ```

4. Tear down any deployment inside the Incus host, then stop the Incus host from the machine.

   ```sh
   make -C deploy down               # inside the Incus host
   limactl stop ravensport           # on the machine
   ```

5. On the machine, copy the Incus host's disk into one read-only qcow2 file. `qemu-img convert` writes a file that stands alone, without the backing file of Lima's disk. Lima keeps the disk in `~/.lima/ravensport/disk`, and Lima 1 named it `diffdisk`.

   ```sh
   qemu-img convert -p -O qcow2 ~/.lima/ravensport/disk ~/ravensport-base.qcow2
   chmod a-w ~/ravensport-base.qcow2
   ```

6. Start a living port from the copy, with the address from step 3 in place of `MAC`.

   ```sh
   python3 -m competition.living create lp1 --base-disk ~/ravensport-base.qcow2 \
       --mac MAC --tree . --training-volume ~/volumes/learning --build
   ```

The guest keeps its user, its key and its network configuration on the disk, so a base disk made this way needs no `--cidata`. `--build` rebuilds, inside the living port, only the images whose inputs differ from those on the base disk, so a base disk made from the current tree gives the shortest `create`. The Incus host starts again as before, since the base disk is a separate file.

On macOS with an Apple processor:

1. On the machine, install QEMU with Homebrew. The package holds QEMU's disk tool and the aarch64 firmware, `share/qemu/edk2-aarch64-code.fd`, where the living port manager finds it, so the manager needs no `--firmware`.

   ```sh
   brew install qemu
   ```

2. Inside the Incus host, from the repository's root, build the competition images. Then stop the package timers, so that no unattended upgrade restarts Incus inside a living port.

   ```sh
   make -C deploy install
   make -C deploy stage-attacks
   make -C deploy build EXTRA_OVERRIDE=../competition/deploy/override.yaml
   sudo systemctl mask --now apt-daily.timer apt-daily-upgrade.timer
   ```

3. Inside the Incus host, read the network card's address. The value after `macaddress` is the address that `--mac` takes, and the address of `eth0` is the same.

   ```sh
   sudo grep macaddress /etc/netplan/50-cloud-init.yaml
   cat /sys/class/net/eth0/address
   ```

4. Tear down any deployment inside the Incus host, then stop the Incus host from the machine.

   ```sh
   make -C deploy down               # inside the Incus host
   limactl stop ravensport           # on the machine
   ```

5. On the machine, copy the Incus host's disk. On macOS, Lima runs the Incus host with Apple's `Virtualization.framework`, Lima's machine type `vz`, and keeps its disk in `~/.lima/ravensport/disk` as one raw file with no backing file. `cp -c` clones the file at once on the Mac's file system, and the clone takes no space until one of the two files changes. The base disk stays raw: the living port manager reads its format with `qemu-img info` and never writes it.

   ```sh
   cp -c ~/.lima/ravensport/disk ~/ravensport-base.raw
   chmod a-w ~/ravensport-base.raw
   ```

6. Start a living port from the copy, with the address from step 3 in place of `MAC`.

   ```sh
   python3 -m competition.living create lp1 --base-disk ~/ravensport-base.raw \
       --mac MAC --tree . --training-volume ~/volumes/learning --build
   ```

The living port boots the copy under QEMU with Apple's Hypervisor framework. The guest keeps its user, its key and its network configuration on the disk, so `create` needs no `--cidata`, and Lima's `cidata.iso` stays with Lima. Lima's guest agent cannot reach its host under QEMU, and `create` stops and masks the agent when it holds the guest's clock, so the copy needs no change by hand. Where your user's name is not a valid Linux user name, such as a name with a dot, Lima names the guest's user `lima`, and `create` then takes `--user lima`.

On an Intel Mac, whose guests are x86_64, the living port manager runs QEMU's Q35 machine with QEMU's own x86_64 firmware. When it creates the living port, the manager switches the guest's clock source to the high precision event timer (HPET). The base disk tested there was a qcow2 copy made on Linux by the steps above.

## Episode records

The episode records of a living port's forks, and the versions of the training volume, are described in [the entry guide](entry-guide.md#episode-records).

# General deployment

This spec file covers what the deployment must achieve at a minimum, its required properties and the profiles we 
need. Aspects like topology, firewall policy, render pipeline, startup 
sequence will be specified in lower level spec docs and is out of scope here.

## 1. Profiles

Three primary profiles for now (release, test and competition), plus a deferred development profile. Separating them keeps the release profile strictly locked down without constraining test flexibility.

### 1.1 Release

The locked-down target deployment. It must have the following properties:

- No internet access in the Lima virtual machine or its containers.
- No networking between the Lima virtual machine and its containers.
- Fully pre-built container images: no configuration or file movement at
  start time, other than the sim's optional read-only state checkpoint
  (`deployment.md` §8.1).
- A single Makefile target that builds all container images.
- A single Makefile target that deploys all containers and confirms correctness.
- Containers can be switched off and brought back up quickly.
- A stopped environment, with its operational history, can be exported to a
  single file and restored from that file, on another machine of the same
  architecture, with a single Makefile target.

The release profile can be deployed using either:
`ci-minimal` (default) and `full`, selected by `PROFILE` in `deploy/Makefile`:
`ci-minimal` runs every node as a container for portability, while `full` runs
the heavier nodes as real virtual machines for fidelity. See the Deployment
section of [`docs/testbed.md`](../../docs/testbed.md) for the full description.

### 1.2 Test

The release profile plus the minimum extras to keep tests runnable.

- `make test` runs all tests; sub-targets `make test-fms`, `make test-hmi`,
  `make test-sim` run one component's.
- Unit tests need no infrastructure (Rust only).
- Integration tests run via the Lima virtual machine.
- Re-running `make test` reuses build artefacts when nothing has changed.

### 1.3 Competition

The release profile plus the additions that the RavensPort competition needs, which leave the release profile unchanged. `episodes.md` specifies the competition's episodes, each one scored run of RavensPort, and the competition's two modes, training and evaluation.

- The deployment creates `siem-01`, `sim-host` and the three firewall nodes from competition images, which hold the competition's software and settings.
- With the competition's additional override file, which the Makefile variable `EXTRA_OVERRIDE` names, the single Makefile target that builds all container images also builds the competition images, so they are fully pre-built.
- The deployment attaches the simulator's start-up input to `sim-host`, read-only, before `sim-host` starts. The start-up input holds the seed of the simulator's tug arrival generator and the time of the reconciliation user's first audit.
- The deployment attaches one storage volume to `siem-01`. In evaluation, the volume is read-only and attached when the deployment is created. In training, the volume is read-write and attached at the start of each episode.
- The start-up input and the storage volume are disk devices separate from every root filesystem, so every root filesystem starts as its image built it.


### 1.4 Development (deferred)

A possible fourth profile, introduced only if the image rebuild time becomes an issue as we continue to develop the environment. The development profile is deferred until then.

## 2. Startup Preconditions

Ensuring that the services the sim binary depends on are up and reachable before
the sim binary starts is the deployment's responsibility, not the sim's.

In the competition profile, the deployment also attaches the simulator's start-up input to `sim-host` before the sim binary starts.


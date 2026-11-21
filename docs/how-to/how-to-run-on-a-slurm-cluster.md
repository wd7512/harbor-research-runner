# How to run on a Slurm cluster

hrr needs a Docker API, a reachable model gateway and storage that survives between jobs. On a
shared cluster none of these can be assumed, so probe a node before planning a study. The
scripts are in `scripts/slurm/`:

| File | Purpose |
| --- | --- |
| `probe-host.sbatch` | One short job that reports what a compute node offers. |
| `run-study.sbatch` | Runs or resumes one study inside one allocation. |
| `docker-engine.sh` | Shared by both: starts a Docker API inside the allocation. |

Prefer a partition whose CPU architecture matches the task images (amd64 for most task
sources). On an Arm partition every task image must be built natively (`tasks.images: build`).

## 1. Probe a node

From a login node, in a checkout of this repository:

```sh
HRR_GATEWAY_URL=https://your-gateway.example/v1 \
  sbatch --partition=PARTITION scripts/slurm/probe-host.sbatch
```

Add the site's account and QoS options. The probe sends no key. It writes
`hrr-probe-<job>/summary.txt` (one `name=value` line per check, with a log for each) in the
submit directory, or in `HRR_PROBE_DIR`. Every check runs even when an earlier one fails.

Read the summary in this order:

| Line | Needed | Meaning |
| --- | --- | --- |
| `engine` | not `none` | Which Docker API started: `existing` (a site daemon), `podman` (Podman's Docker-compatible service) or `rootless-docker` (a static Docker release started inside the job). `none` ends the probe: hrr cannot run on this node. Set `HRR_ENGINE` to force one. |
| `compose` | `0` | Harbor runs trials through `docker compose`; the scripts download the plugin when the engine lacks it. |
| `net_gateway`, `net_docker_hub`, `net_ghcr` | an HTTP status, not `000` | `000` means the node cannot reach that host. Registries answer `401` when reachable. Without registry access, load images from a `docker save` archive. |
| `pull`, `run_host_network` | `0` | The engine can pull and run a container with host networking. |
| `bridge_network`, `run_default_network` | either | Without a bridge, hrr uses host networking for task containers; set `proxy.container_host` (step 3). Tasks whose Compose files add sidecar services need a bridge and are excluded at machine check. |
| `memory_cap` | either | `kernel`: Docker enforces each task's memory limit. `none`: it cannot (usual for rootless engines inside a Slurm cgroup), and hrr's memory watchdog enforces the limit instead. |
| `bind_mount_persistent` | `0`, or use `HRR_RUNS_LOCAL=1` | Whether containers can mount the persistent directory. Rootless user namespaces often cannot read parallel file systems such as Lustre. |
| `reach_host_127.0.0.1`, `reach_host_10.0.2.2`, `reach_host_gateway` | at least one `0` | Which address lets a task container reach a service on the job host, which is how agents reach the hrr proxy. The first that returns `0` is the `proxy.container_host` value (`host-gateway` means `host.docker.internal`). |

Set `HRR_REF` to a release tag to also check that hrr installs on the node.

## 2. Prepare the study folder

On persistent storage, create the study folder with the job file, the harness manifest and,
outside any repository, a key file of `KEY=value` lines naming the model card's `key_env`
variables, with mode `600`. Never pass a key on the command line.

Run the machine check inside an allocation, once per host and engine; see
[How to check a new host](how-to-check-a-new-host.md). Every pass of a run must see the same
host profile, so keep the partition and engine fixed for the life of a run.

To run that check with the engine set up by `run-study.sbatch`, submit it before the study with
`HRR_COMMAND=check-machine`:

```sh
HRR_COMMAND=check-machine HRR_STUDY_DIR=/path/to/study HRR_REF=v0.1.1 \
  HRR_ENV_FILE=/path/to/key.env sbatch --partition=PARTITION scripts/slurm/run-study.sbatch
```

Inspect `checks/machine-check.json` and compare its `included` tasks as described in
[How to check a new host](how-to-check-a-new-host.md) before starting the study.

## 3. Job file settings for the host

- `proxy.container_host`: the address from the probe's `reach_host_*` lines.
- `tasks.images: build` when the node cannot run the task images' architecture.
- `limits.memory_reserve_mb`: memory kept free outside task containers. On a `memory_cap:
  none` node the Slurm allocation is the only hard boundary, and Slurm may kill the whole job,
  Docker daemon included, when it is exceeded. Request the node's memory explicitly
  (`--mem` or `--exclusive`).

## 4. Run across allocations

```sh
HRR_STUDY_DIR=/path/to/study HRR_REF=v0.1.1 HRR_ENV_FILE=/path/to/key.env \
  sbatch --partition=PARTITION scripts/slurm/run-study.sbatch
```

The script installs the pinned hrr, starts the engine, and computes a stop time
`HRR_STOP_MARGIN_MIN` minutes (default 120) before the allocation's end; choose a margin longer
than the longest agent budget plus its verifier timeout and teardown. It runs
`hrr run --stop-launching-at`, and records the run id in `.hrr-run-id` when hrr exits `2`
(resumable). Submit the same command again: the next allocation resumes that run. Exit `0`
(complete) or `3` (finished incomplete) clears the run id. Each job appends a line to
`slurm-logs/jobs.txt`.

Set `HRR_RUNS_LOCAL=1` when `bind_mount_persistent` failed: the runs folder lives under
`/tmp/hrr-runs/<study>` during the job and is copied to persistent storage at the end. The path
is the same in every job, so resumed runs see the same run folder path.

Do not loop on exit `2` inside one allocation: the stop time has passed, and hrr refuses it.

## Known working configuration

Rootless Docker 29.9.3 on Isambard-AI (aarch64) used: Compose v2.39.4 as a CLI plugin under `$DOCKER_CONFIG`, no bridge, no iptables,
`native.cgroupdriver=cgroupfs`, the `vfs` storage driver with job-local data, host loopback
enabled (task containers reach the host at `10.0.2.2`), and `DETACH_NETNS=false`. `docker-engine.sh`
starts the daemon with exactly these settings. Keep Docker data and work folders on node-local
`/tmp`; a site's `$LOCALDIR` may not exist on every node.

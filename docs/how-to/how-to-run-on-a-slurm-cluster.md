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
| `storage_driver` | a Docker storage driver | The driver reported by `docker info`; set `HRR_STORAGE_DRIVER` to `auto` (default) or a specific driver. |
| `compose` | `0` | Harbor runs trials through `docker compose`; the scripts download the plugin when the engine lacks it. |
| `net_gateway`, `net_docker_hub`, `net_ghcr` | an HTTP status, not `000` | `000` means the node cannot reach that host. Registries answer `401` when reachable. Without registry access, load images from a `docker save` archive. |
| `pull`, `run_host_network` | `0` | The engine can pull and run a container with host networking. |
| `bridge_network`, `run_default_network` | either | Without a bridge, hrr uses host networking for task containers; set `proxy.container_host` (step 3). Set concurrency to 1 on a host-network engine because concurrent task containers share host ports and fixed-port tasks can collide, unless every included task is known not to listen on a fixed port and the job declares `tasks.host_ports: none`. Tasks whose Compose files add sidecar services need a bridge and are excluded at machine check. |
| `memory_cap` | either | `kernel`: Docker enforces each task's memory limit. `none`: it cannot (usual for rootless engines inside a Slurm cgroup), and hrr's memory watchdog enforces the limit instead. |
| `bind_mount_persistent` | `0`, or use `HRR_RUNS_LOCAL=1` | Whether containers can mount the persistent directory. Rootless user namespaces often cannot read parallel file systems such as Lustre. |
| `reach_helper` | `0` | The host's temporary HTTP server started and answered a local request. When it is `1`, all `reach_host_*` checks are `skipped`, so a refused connection cannot be mistaken for a failed address. |
| `reach_host_127.0.0.1`, `reach_host_10.0.2.2`, `reach_host_node_ip`, `reach_host_gateway` | at least one `0` | Which address lets a task container reach a service on the job host, which is how agents reach the hrr proxy. The first that returns `0` is the `proxy.container_host` value (`host-gateway` means `host.docker.internal`). With the rootless engine from `docker-engine.sh`, host-network containers usually reach the job host at `10.0.2.2` (RootlessKit's host loopback). |

Set `HRR_REF` to a release tag to also check that hrr installs on the node.
When rootless Docker starts, `auto` tries `overlay2` before `vfs` because overlay2 avoids copying the full filesystem for every image layer and container; set `HRR_STORAGE_DRIVER` to select a driver explicitly.
The rootless engine wraps `runc` so processes started with `docker exec` use umask `022`, which lets services such as `sshd` create files with safe permissions.

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

A saved check can be reused for later runs on the same host, including runs of other studies,
without running `check-machine` again. `hrr run` re-validates the check at launch and refuses to
start (as a failed launch check) when the task source, the Harbor version, the recorded host
profile, or a job concurrency above the checked value no longer matches; for `path` task sources
it also refuses when any task's content has drifted. A different node or Docker engine changes the
host profile, so an out-of-date check is caught rather than used silently. Reuse is therefore safe
when the run is pinned to the same node and engine and the Harbor version is unchanged; rerun
`check-machine` whenever any of these change.

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

## Registry limits

Set `HRR_REGISTRY_AUTH` to a mode `600` file with one `REGISTRY USERNAME TOKEN` line per
registry (for example, `docker.io USERNAME TOKEN`) to log in before Harbor pulls task images.
Each line must hold exactly three whitespace-separated fields: the scripts refuse to start with
`registry credentials lines must have exactly three fields` when a line is missing a field, is a
bare token, or carries trailing text. Point `HRR_REGISTRY_AUTH` at a file in this format, not at a
Docker `config.json` or a single-token file.
Set `HRR_IMAGE_CACHE` to a persistent directory to load cached `.tar` images at job start and
save newly pulled images before the engine stops. A single full pull fills the cache for later
jobs; point subsequent allocations at the same directory to reuse it. The cache must also hold the
harness image(s) named in `harness-manifest.toml`, not only the task images: with
`tasks.images: prebuilt` hrr loads the harness image from the cache rather than building or pulling
it, and the `arms_pinned` launch check fails with `local image '<ref>' is missing` when it is
absent. Save the harness image tar (from `hrr build-harness`) into the cache directory alongside
the task images.

## Replay arms and the canary

hrr runs a one-trial canary per arm before the scored run and refuses to start unless each arm's
canary trial is valid and scores reward `1.0`. The canary task is `say-hello`.

An arm that sets `instruction_variants` — for example a plan-replay arm whose saved plan replaces
each task's instruction — applies that map to the canary too. The map must therefore also contain
an entry for the `say-hello` instruction, or the agent raises `instruction <hash> has no variant`
and the canary trial is invalid. Map the canary instruction's `sha256` to the canary instruction
text itself (an identity passthrough); the entry is never used by a scored task.

A canary trial can also be marked invalid for transient reasons, such as `upstream_slow` (the proxy
judged the model-gateway response too slow). This is not a configuration error; resubmit the run.

## Known working configuration

Rootless Docker 29.9.3 has run studies on aarch64 and x86_64 Slurm clusters with:
- Compose v2.39.4 as a CLI plugin under `$DOCKER_CONFIG`;
- no bridge and no iptables;
- `native.cgroupdriver=cgroupfs`;
- the `overlay2` storage driver where the kernel allows it, else `vfs`, with job-local data;
- host loopback enabled, so task containers reach the host at `10.0.2.2`;
- `DETACH_NETNS=false`.

`docker-engine.sh` starts the daemon with exactly these settings. Keep Docker data and work folders on node-local
`/tmp`; a site's `$LOCALDIR` may not exist on every node.

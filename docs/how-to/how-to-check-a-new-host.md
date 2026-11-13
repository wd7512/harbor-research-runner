# How to check a new host

Run these stages on each host before its first paid study. They use `study.yaml`, `checks/machine-check.json`, `runs/` and `evidence/` in the study repository. The examples assume the study job and its task source are already pinned; replace the sample commit and task names with the study's values.

## A. Build each harness image

Require no swap for containers. In Docker Desktop, set Settings → Resources → Advanced → Swap to `0`; on Linux, run with swap off using `swapoff -a`. Record the machine's RAM against the Docker VM's memory. Swap occupied before a run does not matter; only swapping during a trial does.

Run once for every OpenCode commit listed as an arm in `study.yaml`:

```sh
uv run hrr build-harness --harness opencode --commit 0123456789abcdef0123456789abcdef01234567
```

Read `harness-manifest.toml`. Under `[opencode."<commit>"]`, `version` is the version returned by running `opencode --version` inside the built image. Under `[opencode."<commit>".images.<arch>]`, confirm `ref` and `digest` identify the image recorded for this host's architecture. To inspect the image directly, run `docker run --rm <ref-from-manifest> opencode --version`; its output must match that `version` and the version for the pinned upstream commit. Repeat for every study commit.

## B. Check the task set at study concurrency

Use the study job's task source and `concurrency`. Add `--with-do-nothing` so the machine check measures both the oracle and do-nothing agent:

```sh
uv run hrr check-machine --job study.yaml --out checks/machine-check.json --with-do-nothing
```

Confirm `checks/machine-check.json` exists and inspect `task_source`, `concurrency`, `tasks`, and `included`. Compare `included` with the included task names from a previous oracle sweep using the same task source and concurrency. Investigate and record the reason for every difference before proceeding; a task is included only when its oracle reward is `1.0` and its do-nothing reward is `0.0` where measured.

The job file's `limits.memory_reserve_mb` can be raised or lowered for each host to reflect memory that must remain available outside task containers.

The job file's `limits.memory_reserve_mb` can be raised or lowered for each host to reflect memory that must remain available outside task containers.

## C. Run a free-model pilot

Copy `study.yaml` to a pilot job file. Keep its task source, concurrency, arms, and other settings; change only `repetitions` to `1`, set `tasks.include` to five names from `checks/machine-check.json`'s `included` list, and set `model` to a free model card. See [How to add a model](how-to-add-a-model.md) for the model-card fields and credential setup.

Run the pilot:

```sh
uv run hrr run pilot.yaml
```

Require exit code `0`. Find the run folder under `runs/<run-id>/` and the automatically built bundle under `evidence/<run-id>/`. In `evidence/<run-id>/manifest.json`, confirm the bundle lists its files; in `report.md`, confirm `Slots planned` equals `Slots complete`, `Slots incomplete` is `0`, and `Unmeasured trials` is `0`. Open `uv run hrr explore evidence/<run-id>` in a browser and select a trial to inspect its request series. Run `uv run harbor view runs/<run-id>`, open the address it prints, choose the `harbor-job` job and drill into a trial trajectory; set-aside attempts appear as the `set-aside-trials` job. A person must confirm that both views open and show trial details.

## D. Drill halt and resume during a second pilot

Start another pilot with `uv run hrr run pilot.yaml` and let trials begin. In its `runs/<run-id>/run-events.jsonl`, find the most recent `launch` event and read its `pid` field. Verify that PID is the runner with `ps -p <pid> -o pid=,command=`, then stop that runner from the operator's shell:

```sh
kill -9 <runner-pid>
```

Harbor runs under a guard that watches the runner. When the runner dies, the guard sends Harbor SIGTERM, waits up to 30 seconds, then kills Harbor's process group. Until then, `clean-docker --apply` and `--resume` refuse, because a process still names the run folder. Wait until this prints nothing, which usually takes under a minute:

```sh
ps -ww -axo pid=,command= | grep "runs/<run-id>" | grep -v grep
```

Then clean leftover Docker resources and resume the same run:

```sh
uv run hrr clean-docker --apply
uv run hrr run --resume <run-id>
```

If a process still names the folder after two minutes, record its command line as a host defect before killing it. The guard did not stop it, and the pilot does not pass.

Require the resume command to exit `0`. In `runs/<run-id>/run-events.jsonl`, confirm a `resume` event followed by a `finish` event with `exit_code` `0`, `slots_incomplete` `0`, and `slots_complete` equal to the number of planned task-arm slots. In `evidence/<run-id>/report.md`, confirm `Slots planned` equals `Slots complete`, `Slots incomplete` is `0`, and `Unmeasured trials` is `0`. Also read `repetitions` from `runs/<run-id>/run-metadata.json` under `job`; in `evidence/<run-id>/trials.jsonl`, group rows with `location` `harbor-job`, `valid` true, and `measured` true by `task` and `arm`. Every planned task-arm pair must have at least that many rows. This reconciles planned slots with measured valid trials.

To check that no trial was counted twice, inspect `evidence/<run-id>/trials.jsonl`: every `trial` ID must occur once, including rows whose `location` is `set-aside-trials`. Cross-check those IDs against `trial_verdict` events in `run-events.jsonl`; each trial has one verdict event, and any rerun attempt has its own distinct trial ID. Set-aside attempts remain in the evidence bundle but do not count as final valid trials.

# How to run a study

These steps assume you have a pinned task repository, Docker, and a host suitable for Harbor. The example uses one model card and two OpenCode commits; replace the sample task repository URL, task name, and commit hashes with your study's pinned values. A model card's `key_env` entries must be set in the environment before you run the study. For each key variable, use its declared name and store the secret only in your environment; never put a key value in a file.

1. **Build an image for each OpenCode arm.** Repeat this command for each commit. It builds the local image and records it in the manifest.

   ```sh
   uv run hrr build-harness --harness opencode --commit 0123456789abcdef0123456789abcdef01234567
   ```

   The default manifest is `harness-manifest.toml` in the current directory. The command supports `--manifest PATH` when the study uses a different location. The `fake` harness is a test fixture and has no image to build.

   On an arm64 host running amd64 task images (Terminal-Bench 2.x), build with `--arch amd64`.

2. **Write the job file.** `check-machine` reads this file, so create it before running the machine check. Save this example as `study.yaml`:

   ```yaml
   name: harness-comparison
   model: opencodego/mimo-v2.6-pro
   repetitions: 3
   concurrency: 1
   task_order: as_listed
   tasks:
     git:
       url: https://example.org/your-pinned-task-repository.git
       commit: 0123456789abcdef0123456789abcdef01234567
     include: [task-name]
     exclude: []
   machine_check: checks/machine-check.json
   arms:
     - label: baseline
       harness: opencode
       commit: 0123456789abcdef0123456789abcdef01234567
     - label: candidate
       harness: opencode
       commit: 89abcdef0123456789abcdef0123456789abcdef
   proxy:
     host: 127.0.0.1
     port: 8766
     container_host: host.docker.internal
   limits: {}
   ```

   The task source must be exactly one of `dataset`, `path`, or `git`. A git source pins both `url` and a 40-character lowercase commit. `include` selects task names; an empty list means all tasks that pass the machine check. `exclude` removes named tasks from that set. Task names are Harbor task IDs: folder names for `path` and `git` sources, and `org/name` for `dataset` tasks, never `[task].name`. The arms share one model and need unique labels.

   On a Linux Docker Engine host, set `proxy.host` to `0.0.0.0` and `proxy.container_host` to the Docker bridge gateway address reachable from Harbor's Compose network, because Harbor does not add a `host.docker.internal` host mapping.

3. **Check the machine and task set.** The check runs the oracle for the tasks selected by the job and writes the machine-check record used by `hrr run`.

   ```sh
   uv run hrr check-machine --job study.yaml --out checks/machine-check.json
   ```

   Add `--with-do-nothing` to also run the do-nothing agent proof. Use `--runs-dir PATH` if the run folders are stored somewhere other than `runs/`. This command needs Docker and may take time proportional to the selected task set.

4. **Run the study.** The command checks the manifest, model card, key variables, machine check, Docker state, host resources, and task selection before spending on trials.

   ```sh
   uv run hrr run study.yaml
   ```

   The defaults use `runs/`, `evidence/`, and `harness-manifest.toml`. Options are `--runs-dir PATH`, `--evidence-dir PATH`, `--manifest PATH`, and `--no-evidence`. The run includes a real-model canary, so this command requires Docker and working model credentials. Exit code 0 means all planned slots have enough valid, measured trials; 2 means the run halted and can be resumed; 3 means it finished with incomplete slots; 1 means it refused to start or had an internal error. Evidence is built automatically after exit 0 or 3 unless `--no-evidence` was supplied.

5. **Resume after a halt.** Use the run folder name shown in the halt output or under `runs/`.

   ```sh
   uv run hrr run --resume harness-comparison-20261112T101500123456Z
   ```

   The resume repeats launch checks and does not repeat the canary. It uses the same run folder and job settings. As with a new run, it needs Docker and any required model key variables.

6. **Build or rebuild the evidence bundle.** This can also be run for an unfinished run to include trials judged so far.

   ```sh
   uv run hrr build-evidence harness-comparison-20261112T101500123456Z
   ```

   `--runs-dir PATH` and `--evidence-dir PATH` select non-default folders. A secret scan failure returns 4 and writes no bundle.

7. **Explore evidence.** Start the read-only browser over the default `evidence/` folder, or pass one or more bundle paths.

   ```sh
   uv run hrr explore evidence
   ```

   Add `--port PORT` to use a different port. Stop the server with Ctrl-C.

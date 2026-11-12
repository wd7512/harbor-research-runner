# Architecture contract

Every step brief refers to this file. If a brief and this file disagree, stop and report; do not choose.

Names used throughout: the package is `harbor_research_runner`, the command is `hrr`. "OLD" means a read-only checkout of the private predecessor of this tool, from which proven code is ported. "Harbor" means the pinned `harbor` package (`harbor==0.23.0`).

## 1. Modules

A study flows top to bottom through six steps. Five helper modules are shared.

| Kind | Module | Owns | May import (ours) |
|---|---|---|---|
| helper | `records` | The format of every file we write, the proxy control messages, the limits record | nothing |
| helper | `job_config` | The job file and model cards | nothing |
| helper | `trial_validity` | The verdict for one trial; the halt rules for a run | `records` |
| helper | `statistics` | Pass rates, intervals, paired comparison, completeness table | `records` |
| helper | `harbor_adapter` | Harbor command lines, reading Harbor's job folder, the set-aside move, the Harbor plugin | `records`, `job_config`, `trial_validity`, `docker_host` |
| helper | `docker_host` | Host lock, Docker cleanup, resource check, resource sampler | `records` |
| step | `harness` | Commit manifest, image build, extraction, `PinnedHarness`, `OpenCodeAgent`, the fake harness | `records` |
| step | `run` | Launch checks, canary, machine check, the run loop | `job_config`, `records`, `trial_validity`, `harbor_adapter`, `docker_host`, `harness` |
| step | `model_proxy` | The relay between harness and model; writes the proxy ledger | `records` |
| step | `evidence_bundle` | Run folder to evidence bundle, secret scan, report | `records`, `statistics`, `harbor_adapter` |
| step | `results_explorer` | Read-only comparison server and page | `records`, `statistics` |
| wiring | `cli` | Commands | any module |

Rules, enforced by `tests/unit/test_import_rule.py`:

1. A step never imports another step. The one exception is `run` importing `harness`.
2. A helper never imports a step.
3. `records` and `job_config` import nothing of ours.
4. Harbor's Python package is imported in exactly three files: `harness/agents.py`, `harness/fake.py` and `harbor_adapter/plugin.py`.
5. `run` starts `model_proxy` as a child process (`python -m harbor_research_runner.model_proxy --config FILE`). It never imports it.

Source layout:

```
src/harbor_research_runner/
  cli.py
  records/          jsonl.py  proxy_ledger.py  proxy_control.py  run_metadata.py
                    run_events.py  resource_stats.py  trials.py  limits.py  machine_check.py
  job_config/       job_file.py  model_cards.py  model_cards/<provider>/<model>.json
  trial_validity/   verdict.py  upstream.py  phase_table.py  run_health.py
  statistics/       rates.py  paired.py  completeness.py  cost.py
  harbor_adapter/   job_folder.py  commands.py  set_aside.py  plugin.py
  docker_host/      host_lock.py  cleanup.py  resources.py  sampler.py
  harness/          manifest.py  extract.py  agents.py  opencode_eras.py  fake.py  fake_script.py
                    build.py  build/
  model_proxy/      __main__.py  server.py  ledger.py  relay.py  status.py  usage.py
                    chars.py  timing.py  pacing.py  keys.py  control.py  bodies.py  adapters/
  run/              checks.py  canary.py  machine_check.py  loop.py  watch.py  process_group.py
                    canary_task/say-hello/
  evidence_bundle/  build.py  trials.py  manifest.py  secret_scan.py  report.py
  results_explorer/ loader.py  server.py  page.html
tests/  unit/  integration/  e2e/  live/  fixtures/  support/
```

Unit tests mirror the source path: `src/harbor_research_runner/records/jsonl.py` is tested by `tests/unit/records/test_jsonl.py`.

## 2. Commands

| Command | Does | Exit codes |
|---|---|---|
| `hrr build-harness --harness opencode --commit SHA` | Builds the image for one commit and adds it to the commit manifest | 0 ok, 1 failed |
| `hrr check-machine --job JOB.yaml --out FILE` | Runs the oracle check for the job's task set on this machine | 0 ok, 1 failed |
| `hrr run JOB.yaml` / `hrr run --resume RUN_ID` | Launches or resumes a run | 0 complete, 1 refused to start or internal error, 2 halted and resumable, 3 finished incomplete |
| `hrr build-evidence RUN_ID` | Builds the evidence bundle from a run folder | 0 ok, 1 failed, 4 secret found (nothing written) |
| `hrr explore [PATH ...]` | Starts the results explorer over evidence bundles | 0 |
| `hrr clean-docker [--apply]` | Lists or removes leftover trial containers and networks | 0 clean, 1 residue found (without `--apply`) or refused because another process holds the host lock (`--apply`) |

Exit code 0 from `hrr run` means: every planned slot has its full number of valid, measured trials.

## 3. Folders

```
runs/<run-id>/                         evidence/<run-id>/
  run-metadata.json   written once       manifest.json
  run-events.jsonl                       run-metadata.json
  runner.log                             run-events.jsonl
  harbor-job-config.yaml                 trials.jsonl
  harbor-job/         Harbor's           proxy-requests.jsonl.gz
  canary-job/         Harbor's           resource-stats.jsonl.gz
  set-aside-trials/<trial>/              report.md
  proxy-config.json
  proxy-ledger.jsonl
  proxy.log
  resource-stats.jsonl
  request-bodies/<trial>.jsonl[.gz]
```

`<run-id>` is `<job name>-<UTC timestamp to microseconds>Z`, for example `oc-119-vs-21-20261201T101500123456Z`.

`runs/` is never committed. `evidence/` is committed (in the study repo). Harbor owns every name inside `harbor-job/` and `canary-job/`. A finished run folder (exit 0 or 3) is made read-only.

## 4. Records

All `.jsonl` files hold one JSON object per line, UTF-8, keys sorted, no trailing spaces. Every object has `"type"`. Times named `t` are Unix seconds (float, wall clock, UTC). Durations end in `_s`.

`records/jsonl.py` is the only code that appends to or reads these files:

```python
def append_row(fd: int, row: dict) -> None        # one os.write of a full line
def read_rows(path: Path) -> ReadResult            # rows, plus problems
@dataclass(frozen=True)
class ReadResult:
    rows: list[dict]
    torn_last_line: bool          # final line unparseable: tolerated, reported
    bad_lines: list[int]          # 1-based numbers of unparseable non-final lines
```

### 4.1 `proxy-ledger.jsonl`

Every row has `seq` (integer, starts at 1, +1 per row, never reused across proxy restarts in one run) and `t`.

| `type` | Written | Other fields |
|---|---|---|
| `proxy_start` | When the proxy process starts | `proxy_version`, `config_sha256`, `restart` (bool) |
| `open` | On `POST /control/open` | `trial`, `task`, `arm` |
| `request_start` | Before the request is forwarded upstream | `trial`, `request_id`, `model`, `is_stream`, `prompt_chars`, `paced_wait_s` |
| `request_end` | Before the last bytes are released to the harness | `trial`, `request_id`, `http_status`, `upstream_status`, `upstream_http_status`, `served_model`, `prompt_tokens`, `completion_tokens`, `cached_read_tokens`, `completion_chars`, `first_chunk_s`, `first_content_s`, `duration_s`, `failovers`, `error_excerpt` |
| `refused` | A request arrives for a trial that is closed or was never opened | `trial`, `reason` (`closed` or `not_open`) |
| `close` | On `POST /control/close` | `trial` |

`upstream_status` is one of:

| Value | Meaning | Counts against the trial |
|---|---|---|
| `ok` | The harness received a complete response | no |
| `rate_limited` | 429 that is not a quota error reached the harness | yes |
| `quota_exhausted` | The provider says the account quota is used up | yes |
| `server_error` | 5xx reached the harness | yes |
| `stalled` | The proxy cut the request: no first chunk within `first_chunk_max_s`, a gap over `inter_chunk_gap_max_s`, or total over `request_latency_max_s` | yes |
| `truncated` | The stream ended without its terminator | yes |
| `transport_error` | Connection to the upstream failed | yes |
| `client_error` | Other 4xx: the harness sent a bad request | no |
| `client_disconnect` | The harness closed the connection first | no |

A failover that the proxy absorbed (a later key or endpoint answered) is an `ok` row with `failovers > 0`.

Write policy: flush after every row; `fsync` after every `close` row and at least every 5 seconds, from a background thread. If a write or flush fails, the proxy stops serving (every later request gets HTTP 503 with body `proxy ledger unavailable`) and exits with code 70.

Each relayed response owns its upstream connection. Closing the response, or closing its chunk iterator, releases that connection even before iteration begins. The proxy server calls `close()` from a `finally`.

### 4.2 Proxy control (`records/proxy_control.py`)

The harness reaches the model at `http://<host>:<port>/trial/<trial>/v1/...`. `<trial>` is Harbor's trial name.

| Call | Body | Reply |
|---|---|---|
| `GET /control/health` | | `{"ok": true, "seq": N}` |
| `POST /control/open` | `{"trial", "task", "arm"}` | 200; opening an open trial is a no-op; opening a closed trial is 409 |
| `POST /control/close` | `{"trial"}` | 200; closing twice is a no-op |

A request for a trial that is not open gets HTTP 410 with body `{"error": {"message": "trial closed", "type": "trial_closed"}}`. The words `rate limit`, `too many requests`, `quota`, `usage limit` and `credits` must not appear in any proxy-generated error body (Harbor matches those in agent output).

`proxy-config.json` fields: `host`, `port`, `ledger_path`, `bodies_dir` (or null), `model` (the model card, embedded), `limits` (section 4.6), `restart_cooldown_s`.

### 4.3 `run-metadata.json`

Written once, atomically, before anything else starts: write a temporary file in the same folder, `fsync` it, then link it into place without replacing any existing destination. An existing destination, including a dangling symlink, raises `FileExistsError` and is left unchanged. Never rewritten.

Fields: `run_id`, `created_t`, `tool_version`, `tool_commit`, `harbor_version`, `job` (the parsed job file, verbatim), `job_file_sha256`, `model_card` (embedded), `arms` (list of `label`, `harness`, `commit`, `image_ref`, `image_digest`, `build_method`), `task_set` (`ref`, `tasks`: list of `name`, `digest`), `machine_check_sha256`, `host` (`fingerprint`, `arch`, `cpus`, `memory_mb`, `docker_version`), `proxy` (`host`, `port`), `limits`.

`tool_version` is the installed `harbor-research-runner` package version. `tool_commit` is the 40-hex commit of the checkout containing the package, with `+dirty` appended when tracked files have changes; it is `none` when Git is unavailable or the package is not in a Git checkout.

### 4.4 `run-events.jsonl`

Each row has `t` and `type`:

| `type` | Fields |
|---|---|
| `launch` | `tool_version`, `tool_commit`, `pid` |
| `launch_check` | `name`, `ok`, `detail` |
| `harbor_start` | `pass` (0 for `harbor run`, then 1, 2, ...), `command`, `job` (`canary` or `main`) |
| `harbor_exit` | `pass`, `returncode`, `job` |
| `trial_verdict` | `trial`, `task`, `arm`, `valid` (bool), `reason`, `instrument`, `labels` (list), `measured` (bool), `unmeasured_reason` |
| `set_aside` | `trial`, `task`, `arm`, `reason` |
| `halt` | `reason`, `detail` |
| `resume` | `tool_version`, `tool_commit`, `pid` |
| `finish` | `exit_code`, `slots_complete`, `slots_incomplete` |

A `trial_verdict` is written exactly once per trial, by `run`, when the trial has finished or is found unfinished at a halt or resume. Nothing else decides validity.

### 4.5 `resource-stats.jsonl`

One row every 10 seconds, `type` = `sample`: `t`, `mem_available_mb`, `swap_used_mb`, `disk_free_gb`, `load1`, `trial_containers`, `docker_ok` (bool).

### 4.5a Machine check file (`records/machine_check.py`)

Written by `hrr check-machine`, read by the launch checks. Fields: `created_t`, `tool_version`, `harbor_version`, `host_fingerprint`, `task_source` (the job file's `tasks` block without `include`/`exclude`), `concurrency`, `tasks` (list of `name`, `digest`, `oracle_reward`, `agent_s`, `verifier_s`, `verifier_budget_s`, `nothing_reward` or null), `included` (task names with `oracle_reward == 1.0`, and `nothing_reward == 0.0` where measured), `verifier_timeout_multiplier`.

### 4.6 Limits (`records/limits.py`)

One record, declared in the job file under `limits`, copied into `run-metadata.json` and `proxy-config.json`. Defaults in brackets.

Upstream: `tolerated_errors` [1], `tolerated_error_fraction` [0.02], `trial_output_rate_min` [2.0 tokens/s], `first_chunk_max_s` [180], `inter_chunk_gap_max_s` [180], `request_latency_max_s` [1200], `halt_after_consecutive_invalid` [5], `zero_billed_window_s` [900].

Host: `min_spare_memory_mb` [2048], `max_swap_used_mb` [512], `min_free_disk_gb` [20], `host_breach_halt_s` [120], `launch_min_interval_s` [0].

Run: `max_set_asides_per_slot` [2 x repetitions], `verifier_timeout_multiplier` [from the machine check], `inactivity_margin_s` [600].

### 4.7 `trials.jsonl` (evidence bundle)

One row per trial, including set-aside trials and canary trials. Sorted by `task`, `arm`, `agent_started_t`, `trial`.

Identity: `trial`, `task`, `arm`, `harness`, `commit`, `model`, `location` (`harbor-job`, `set-aside-trials` or `canary-job`), `is_first_attempt`, `is_final`.

Outcome: `reward` (number or null), `is_pass` (`reward == 1.0`), `harbor_exception` (Harbor's label, verbatim, or null), `agent_exit_nonzero` (bool).

Phases: `environment_setup_started_t`, `environment_setup_finished_t`, `agent_setup_started_t`, `agent_setup_finished_t`, `agent_started_t`, `agent_finished_t`, `verifier_started_t`, `verifier_finished_t`.

Validity: `valid`, `reason`, `instrument`, `labels`, `measured`, `unmeasured_reason` (copied from the `trial_verdict` event).

Usage: `requests`, `error_requests`, `refused_requests`, `prompt_tokens`, `completion_tokens`, `cached_read_tokens`, `prompt_chars`, `completion_chars`, `cost_usd_estimate`, `harbor_reported_tokens`.

Behaviour: `turns`, `tool_calls`, `median_first_content_s`, `output_rate_tokens_per_s`, `agent_wall_s`.

`is_first_attempt`: for each (task, arm), order every non-canary trial by `agent_started_t` (trials that never started the agent sort by `environment_setup_started_t`); the first K are first attempts, where K is the job's `repetitions`.

`is_final`: the trial is in `harbor-job/` and `valid` is true.

## 5. Job file

```yaml
name: oc-119-vs-21
model: opencodego/mimo-v2.6-pro          # a model card id
repetitions: 3
concurrency: 8
task_order: slowest_first                 # or as_listed
tasks:
  dataset: terminal-bench/terminal-bench-2-1   # exactly one of dataset / path / git
  # path: ../tb21-fixed
  # git: {url: https://..., commit: <40 hex>}
  include: []                             # empty means all tasks the machine check included
  exclude: []
machine_check: checks/i5-tb21-m8.json
arms:
  - {label: oc-1.19.3, harness: opencode, commit: <40 hex>}
  - {label: oc-2.1.4,  harness: opencode, commit: <40 hex>}
proxy: {host: 127.0.0.1, port: 8766, container_host: host.docker.internal}
limits: {}                                # overrides for section 4.6
```

Unknown keys are an error. Arm labels match `[a-z0-9][a-z0-9.-]*` and are unique. Every arm uses the one `model`.

An arm with `harness: fake` may carry one opaque `fake` mapping. The fake harness validates its `requests`, `exit_code`, `sleep_s`, `ignore_stop`, `keep_requesting_s` and `solve` fields; the job parser keeps the mapping opaque. Every other harness rejects `fake`.

A slot is a (task, arm) pair. Each slot needs `repetitions` valid, measured trials.

A model card (`job_config/model_cards/<provider>/<model>.json`) has: `id`, `upstream_model` (the exact identifier sent upstream and expected back), `api` (`chat` or `responses`), `endpoints` (list of base URLs), `key_env` (list of environment variable names), `tokens_per_minute`, `pricing` (`input`, `output`, `cached_read` per million tokens), `variants` (nonempty string ids mapped to nonempty settings dictionaries), `default_variant` (optional, null by default; when set, a string naming a declared variant), `withdrawn` (bool, with `reason`), and an optional `request` object with `drop_message_fields` (list of non-empty strings) and `max_tokens_cap` (positive integer or null). The proxy applies request shaping once, after `build_request` and before pacing. Secrets are read from the environment only and are never written to any file.

## 6. Trial validity

`trial_validity.verdict.judge_trial(...)` is the only function that decides validity. `run` calls it once per trial and writes a `trial_verdict` event.

```python
@dataclass(frozen=True)
class TrialFacts:                 # built by harbor_adapter.job_folder
    trial: str; task: str; arm: str
    has_result: bool
    reward: float | None
    exception_type: str | None
    exception_at: float | None                 # exception_info.occurred_at, Unix seconds
    phase_started: dict[str, float | None]    # environment_setup, agent_setup, agent, verifier
    phase_finished: dict[str, float | None]
    harbor_reported_tokens: int

@dataclass(frozen=True)
class Verdict:
    valid: bool
    reason: str | None            # set when valid is False
    instrument: str | None        # "harbor", "host", "proxy", or None
    labels: tuple[str, ...]
    measured: bool
    unmeasured_reason: str | None

def judge_trial(facts: TrialFacts, ledger_rows: list[dict], ledger_problems: ReadResult,
                resource_rows: list[dict], limits: Limits,
                earlier_set_aside_reasons: tuple[str, ...]) -> Verdict
```

When `exception_type` is not null, the failed phase is the last phase (in order environment_setup, agent_setup, agent, verifier) whose start time is non-null and no later than `exception_at`. If `exception_at` is null, use the last phase with a non-null start time.

Rules, first match wins:

| # | Condition | Verdict |
|---|---|---|
| 1 | `has_result` is false | invalid, `never_finished`, harbor |
| 2 | `exception_type == "CancelledError"` | invalid, `cancelled`, harbor |
| 3 | failed phase is `environment_setup` | invalid, `environment_failed`, harbor |
| 4 | failed phase is `agent_setup` and `harness_setup_failed` is not in `earlier_set_aside_reasons` | invalid, `harness_setup_failed`, harbor |
| 5 | failed phase is `agent_setup` and `harness_setup_failed` is in `earlier_set_aside_reasons` | valid, label `harness_could_not_start` (counts as a failure) |
| 6 | any resource sample between `agent` start and `verifier` finish (or `agent` finish if the verifier never started) breaches a host limit, or has `docker_ok` false | invalid, `host_fault`, host |
| 7 | the trial's last `request_end` (highest `seq`) has a status that counts against the trial | invalid, `upstream_terminal_error`, proxy |
| 8 | counted errors > max(`tolerated_errors`, floor(`tolerated_error_fraction` x N)), where N is the trial's `request_end` rows excluding `client_disconnect` | invalid, `upstream_errors`, proxy |
| 9 | the trial has at least one `ok` row with `completion_tokens > 0`, and total completion tokens / total `duration_s` over `ok` rows < `trial_output_rate_min` | invalid, `upstream_slow`, proxy |
| 10 | `reward` is null and the failed phase is not `verifier` | invalid, `no_verdict`, harbor |
| 11 | otherwise | valid |

Labels added to a valid verdict: `timed_out` (`exception_type == "AgentTimeoutError"`), `exit_nonzero` (any other exception in the `agent` phase), `verifier_failed` (failed phase is `verifier`; the trial counts as a failure with reward 0), `no_requests` (zero `request_start` rows), `possible_bypass` (`harbor_reported_tokens > 0` and zero `request_end` rows), `refused_after_close` (one or more `refused` rows with reason `closed`).

`measured` is false, with a reason, when: the trial has a `request_start` with no matching `request_end` although a `close` row exists (`request_not_recorded`); a `bad_lines` entry or a sequence gap falls between the trial's `open` and `close` rows (`ledger_damaged`); or the trial made requests but has no `open` row (`no_open_row`). An unmeasured trial is not re-run; it stops the run from exiting 0.

Validity never reads `reward`, except rule 10, which reads only whether a reward exists.

### Halt rules

`trial_validity.run_health.should_halt(...)` returns a reason or `None`. `run` calls it every 5 seconds.

| Reason | Condition |
|---|---|
| `quota_exhausted` | any `request_end` row with that status since the last `harbor_start` |
| `upstream_invalid_streak` | the last `halt_after_consecutive_invalid` trial verdicts all have instrument `proxy` and `valid` false |
| `zero_billed_window` | at least one trial is open and no `ok` `request_end` row for `zero_billed_window_s` |
| `proxy_dead` | the proxy process has exited |
| `sampler_dead` | no resource sample for 40 seconds |
| `host_breach` | every sample in the last `host_breach_halt_s` breaches a host limit |
| `inactive` | no ledger row and no new `result.json` for longer than the largest task agent budget x Harbor's timeout multiplier + the largest verifier budget x `verifier_timeout_multiplier` + `inactivity_margin_s` |

Harbor's exception labels are never an input to a halt rule.

## 7. The run loop

1. Launch checks (`run/checks.py`): each writes a `launch_check` event; any failure exits 1 before anything is spent. Checks: the job file parses; every arm's commit is in the manifest, is not withdrawn, and its image digest matches the local image; the model card exists and is not withdrawn; every key variable is set; Docker has no residue; host resources are above the limits; the machine check file exists and matches (task set, Harbor version, machine fingerprint, concurrency not above the checked value); every requested task is in the machine check's included list; this process holds the host lock (D22).
2. Write `run-metadata.json`. Start the resource sampler. Write `proxy-config.json`, start the proxy, wait for `/control/health`.
3. Canary: one Harbor job in `canary-job/` running the bundled task `canary/say-hello` once per arm against the real model. The task lives at `run/canary_task/say-hello/`: its instruction creates `/app/hello.txt` containing `hello`, its environment is `python:3.12-slim` pinned by digest, its agent timeout is 120 s and its verifier timeout is 60 s, and its verifier accepts exactly `hello` or `hello\n`. Pass means every canary trial is valid with reward 1.0 and has at least one `ok` request whose `served_model` equals the card's `upstream_model`. A failure exits 1.
4. Pass 0: `harbor run` with the generated `harbor-job-config.yaml`.
5. Watch: every 5 seconds, judge each newly finished trial, write its `trial_verdict`, and evaluate the halt rules.
6. On halt: stop Harbor's process group, stop the proxy and sampler, judge every remaining trial folder, write `halt`, exit 2.
7. When Harbor exits on its own: judge every remaining trial folder. If any slot has fewer than `repetitions` valid trials and has not reached `max_set_asides_per_slot`, set aside every invalid trial, then run `harbor jobs resume` as the next pass and go to step 5.
8. Finish: stop the proxy and sampler, write `finish`, make the run folder read-only, exit 0 or 3.

Resume (`hrr run --resume RUN_ID`): acquire the host lock, read the run metadata, repeat the launch checks, start the sampler and the proxy (same host and port, `restart: true`, then wait `restart_cooldown_s`), write `resume`, judge and set aside, then continue from step 7. A refused resume appends nothing. The canary is not repeated.

Set-aside (`harbor_adapter.set_aside`): move `harbor-job/<trial>/` to `set-aside-trials/<trial>/` with one `os.rename`, then write the `set_aside` event. Before any move, an existing `set-aside-trials/<trial>/` with no matching event gets its event written (the earlier run crashed between the two). The step can be repeated safely.

Harbor commands (`harbor_adapter.commands`):

```
uv run harbor run --config <run>/harbor-job-config.yaml --jobs-dir <run> --job-name harbor-job \
   --plugin harbor_research_runner.harbor_adapter.plugin:RunPlugin --pk proxy_url=http://<host>:<port> \
   --pk limits_file=<run>/proxy-config.json --pk task_order=<as_listed|slowest_first>
uv run harbor jobs resume -p <run>/harbor-job -f hrr-erases-nothing \
   --plugin harbor_research_runner.harbor_adapter.plugin:RunPlugin --pk proxy_url=... --pk limits_file=... \
   --pk task_order=<as_listed|slowest_first>
```

`-f hrr-erases-nothing` matches no real exception type; it disables Harbor's default of deleting `CancelledError` trials on resume. The generated job config and the proxy address never change between passes or across resumes.

The Harbor plugin (`harbor_adapter/plugin.py`), on Harbor's trial events:

- `START`: wait until spare memory is above the limit and `launch_min_interval_s` has passed since the last launch (ported from OLD's launch gate).
- `AGENT_START`: `POST /control/open` with the trial name, task name and the arm label from the agent's settings.
- `AGENT_END`: `POST /control/close`. This fires when the agent phase ends for any reason, including Harbor's timeout, and before the verifier starts.
- `on_job_start`: when `task_order` is `slowest_first`, stably sort Harbor's remaining trial configs by descending agent budget from each task's `task.toml`; ties keep Harbor's order.

## 8. Harness

```python
class PinnedHarness(ABC):  # harness/agents.py
    harness_name: ClassVar[str]

    @abstractmethod
    def config_files(
        self, facts: HarnessFacts, model: str, proxy_base_url: str
    ) -> dict[str, str]: ...
    @abstractmethod
    def run_command(self, facts: HarnessFacts, model: str, instruction: str) -> str: ...
    @abstractmethod
    async def probe(self, environment) -> HarnessFacts: ...
    @abstractmethod
    def process_pattern(self) -> str: ...


class OpenCodeAgent(
    PinnedHarness, OpenCode
): ...  # Harbor's OpenCode supplies trajectory conversion


class FakeHarnessAgent(PinnedHarness, BaseInstalledAgent): ...  # harness/fake.py, test fixture
```

The rendered config pins sampling to `temperature` 0.6 and `top_p` 0.95. `variants` is the model's declared variant ladder: 1.x emits it as a map keyed by variant id, 2.x as a list of `{id, settings}`; the `default_variant` settings are folded into the model's base `options` or `settings`. A selected or default variant absent from the supplied map is refused before any config is written.

`PinnedHarness` implements `install` (verify the image digest against the manifest, extract the binary, upload it, probe) and `run` (write config files, execute the run command, on cancellation run the stop script before re-raising). The proxy base URL given to the harness is `http://<container_host>:<port>/trial/<trial>/v1`, where `<trial>` is Harbor's `session_id` with the suffix `__agent` removed.

Agent settings (the `kwargs` in Harbor's job config) are produced by `harness.agents.agent_settings(arm, proxy, manifest_path)` and parsed by the agent class. No other module reads or writes them.

The eight settings keys are `arm_label`, `harness`, `commit`, `manifest_path`, `container_host`, `proxy_port`, `variant` and `model_card`. For `harness: fake` only, `agent_settings` accepts a ninth `fake` key; every other harness rejects it. `OpenCodeAgent` declares an options model that adds those keys to Harbor's OpenCode kwargs, so Harbor's preflight and the agent constructor validate the same dictionary; an unknown, missing or mistyped key is refused before a trial is queued, and the local serializer and parser stay authoritative. Config registration, skill registration and the agent command run with a fixed environment — `OPENCODE_FAKE_VCS=git`, `XDG_DATA_HOME=/logs/agent/opencode/xdg-data` and `XDG_STATE_HOME=/logs/agent/opencode/xdg-state` — and no provider credentials are forwarded into the task container.

`FakeHarnessAgent` (`harness/fake.py`) is a test fixture with no image and no manifest: `harness_files` returns a fresh folder holding only `fake_script.py`, `config_files` is empty, `probe` returns fixed facts, `process_pattern` is `fake_script.py`, and `run_command` runs `python3 /usr/local/fake_script.py` with the arm's behaviour. Its typed `fake` behaviour defaults to `requests` 1, `exit_code` 0, `sleep_s` 0, `ignore_stop` false, `keep_requesting_s` 0 and `solve` true; counts and times are nonnegative, and unknown behaviour fields are refused by its options model. The script sends streaming chat requests with a fixed synthetic model ID, may sleep, may write `/app/hello.txt`, may keep requesting while ignoring SIGTERM, and exits with the requested code. HTTP failures exit nonzero, except that the keep-requesting loop continues through refusals until its monotonic duration elapses. Only fixed fixture data is sent; the script takes no API key.

The default `PinnedHarness.harness_files` loads the commit manifest immediately before verifying the arm and extracting the binary. Construction does not read the manifest, so a harness that overrides `harness_files` needs none; ordinary arms still verify before installation.

The commit manifest (`harness-manifest.toml`, path given in the job folder or by `--manifest`):

```toml
[opencode."<40-hex commit>"]
version = "1.19.3"            # informational
withdrawn = false
withdrawn_reason = ""        # optional; shown when a launch is refused
[opencode."<40-hex commit>".images.amd64]
ref = "hrr/opencode:<12-hex>-amd64"
digest = "sha256:..."
build_method = "source"       # or "release-package"
built_at = "2026-12-01"
```

## 9. Evidence bundle

`hrr build-evidence RUN_ID` is a pure function of the run folder: the same run folder gives byte-identical files. It can run on an unfinished run folder and then covers the trials judged so far.

- `trials.jsonl`: section 4.7.
- `proxy-requests.jsonl.gz`: one row per request, `request_start` and `request_end` merged on `request_id`, sorted by `seq` of the start row; gzip with `mtime=0`.
- `resource-stats.jsonl.gz`, `run-events.jsonl`, `run-metadata.json`: copied.
- `report.md`: generated from `statistics`; `evidence_bundle.report.render_report` receives the run metadata, events, planned slots, repetitions, and the loaded `MachineCheck` record.
- `manifest.json`: `tool_version`, `run_id`, and `files` (name, bytes, sha256), written last.

The secret scan runs before anything is written: it searches every output for the literal values of the model card's `key_env` variables and for the patterns ported from OLD's `runtime/secrets.py`. A hit exits 4 and writes nothing.

## 10. Statistics

Three views of every rate, always reported together: `first_attempt` (trials with `is_first_attempt`; an invalid first attempt counts as it scored, a missing reward counts as a failure), `final` (trials with `is_final`), `final_excluding_reruns` (slots in which no trial was set aside).

Per arm: passes, trials, pass rate, Wilson 95% interval. Per task and arm: passes out of valid trials. Paired comparison of two arms: per-task difference in mean reward, its mean over tasks, and a bootstrap 95% interval over tasks (10,000 resamples, seed 0). Completeness table: slots planned, complete, incomplete; trials valid, set aside by reason, unmeasured; tokens spent on set-aside trials.

`statistics.usage_medians(rows, arm)` returns the median requests, prompt and completion tokens, prompt and completion characters, turns, and estimated cost over valid trials for the arm; fields without values return `None`. `statistics.label_counts(rows, arm)` returns verdict-label counts for the arm.

`MachineCheck.excluded_tasks()` returns the sorted task names listed in `tasks` but absent from `included`.

The pure era renderer in harness/opencode_eras.py exposes `config_files(facts, model, proxy_base_url, variant, *, variants=None, default_variant=None)`. The PinnedHarness instance methods keep the abstract signatures above and supply the embedded model card’s variant settings when calling this renderer.

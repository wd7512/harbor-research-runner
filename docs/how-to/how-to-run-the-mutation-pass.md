# Mutation pass

Run the pass with `uv run scripts/mutation_pass.py`. The wrapper installs the mutation tooling and configures mutated-source imports for mutmut's tests, then writes per-mutant results to `.agent/mutation-results.json`.

Mutmut first runs its clean suite and refuses to score mutants when that run fails. In mutmut 3.7.0's `__main__.py`, the runner does this:

```python
clean_test_exit_code = runner.run_tests(mutant_name=None, tests=tests)
if clean_test_exit_code != 0:
    output_catcher.dump_output()
    print("Failed to run clean test")
    exit(1)
```

Its `status_by_exit_code` includes:

```python
1: "killed",
3: "killed",  # internal error in pytest means a kill
0: "survived",
5: "no tests",
2: "check was interrupted by user",
```

The wrapper reads mutmut's per-mutant exit codes and counts only 1 as killed; 0 is survived, and every other code is unverified. The wrapper's pytest report hook also marks a mutant unverified if any test reports that it exceeded its CPU or wall-time budget. The 0.1 second unit CPU budget remains unchanged.

The source-inspection tests are excluded: `tests/unit/test_cli.py::test_cli_does_not_read_environment_variables_by_name`, `tests/unit/test_import_rule.py` and `tests/integration/test_import_rule.py`. A new test that reads source as text must be added to `pytest_add_cli_args_test_selection` in `pyproject.toml`. Mutmut 3.7.0 restricts files but not named functions, so it mutates all of `run/loop.py` and `run/checks.py`.

Run the pass on a case-sensitive filesystem: Linux, or on macOS a case-sensitive APFS disk image with `TMPDIR` pointed inside it. On a case-insensitive disk, a mutant that only upper-cases a file name (`'run-events.jsonl'` to `'RUN-EVENTS.JSONL'`) still opens the same file and survives. The image needs at least 20 GB free, because the host-resources check reads the free space under the test's temporary folder. The wrapper sets `UV_PROJECT_ENVIRONMENT`, disables syncing with `UV_NO_SYNC=1`, and puts `mutants/src` first on `PYTHONPATH`. When running `mutmut run` directly, sync the `mutation` group first, then set those same variables so nested `uv run` commands use the existing environment and import the mutated sources.

For each survivor, write a test that detects the behavior through the public API, or list it below with its reason. A survivor that only a private helper, a private attribute or a patch could kill is a simplification candidate: it marks code the public behaviour does not need.

Equivalent mutants:

### harbor_research_runner.evidence_bundle.trials

- `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_8` and `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_101`: the `UTF-8` codec name is case-insensitive, so both read the same UTF-8 bytes.
- `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_53`: the changed guard only adds memory-map entries with a non-run location or a non-string trial. Those keys are never looked up when constructing a trial row, so no returned row changes.
- `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_93`: Python parses `-INF` and `-inf` as the same negative infinity.
- `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_424` and `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_478`: Python parses `INF` and `inf` as the same positive infinity.

### harbor_research_runner.evidence_bundle.build

- `harbor_research_runner.evidence_bundle.build.x__write_files__mutmut_3`, `harbor_research_runner.evidence_bundle.build.x__write_files__mutmut_5` and `harbor_research_runner.evidence_bundle.build.x__write_files__mutmut_7`: `build` supplies only root-level names to `_write_files`, so recursive creation of nested parents is never needed by this caller.
- `harbor_research_runner.evidence_bundle.build.x_build__mutmut_103`: `UTF-8` and `utf-8` name the same codec.
- `harbor_research_runner.evidence_bundle.build.x_build__mutmut_134` and `harbor_research_runner.evidence_bundle.build.x_build__mutmut_136`: the temporary directory prefix is not part of the bundle or any returned value.
- `harbor_research_runner.evidence_bundle.build.x_build__mutmut_140` and `harbor_research_runner.evidence_bundle.build.x_build__mutmut_141`: the backup directory suffix is internal and is removed after a successful replacement.

### harbor_research_runner.trial_validity.verdict

- `harbor_research_runner.trial_validity.verdict.x__boundary__mutmut_8`: with `or`, the trial's first row becomes the open entry until its open row replaces it; the two differ only for a trial with two open rows, which the proxy never writes because `TrialRegistry.open` returns early for a trial already open.
- `harbor_research_runner.trial_validity.verdict.x__measurement__mutmut_19`: `damaged` starts as `None` instead of `False`; it is only read as a condition, where both are false.
- `harbor_research_runner.trial_validity.verdict.x__position_within__mutmut_2`, `harbor_research_runner.trial_validity.verdict.x__position_within__mutmut_3`, `harbor_research_runner.trial_validity.verdict.x__position_within__mutmut_4`: an invalid row at the open or close position is the trial's own row, and the trial-row check already marks it damaged.
- `harbor_research_runner.trial_validity.verdict.x__positive_seq__mutmut_8`: sequence zero is rejected by `records.proxy_ledger.is_valid_row`; the invalid trial row already marks measurement damaged, so treating its sequence as positive cannot change the public verdict.
- `harbor_research_runner.trial_validity.verdict.x_judge_trial__mutmut_159`, `harbor_research_runner.trial_validity.verdict.x_judge_trial__mutmut_217`: omitting the explicit `None` for `oom_source` uses the `Verdict` field's default `None`.
- `harbor_research_runner.trial_validity.verdict.x_judge_trial__mutmut_220`: in the repeated-setup branch the phase is never `agent` or `verifier`, the only values `_labels` reads, so passing `None` gives the same labels.
- `harbor_research_runner.trial_validity.verdict.x_judge_trial__mutmut_223`, `harbor_research_runner.trial_validity.verdict.x_judge_trial__mutmut_288`: a `False` flag passed to `_labels` becomes `None`; `_labels` only reads it as a condition.

### harbor_research_runner.records.jsonl

- `harbor_research_runner.records.jsonl.x_bad_line_time_spans__mutmut_15`: the extra position is truncated by the zip against the rows.
- `harbor_research_runner.records.jsonl.x_bad_line_time_spans__mutmut_21`, `harbor_research_runner.records.jsonl.x_bad_line_time_spans__mutmut_24`, `harbor_research_runner.records.jsonl.x_bad_line_time_spans__mutmut_25`: the positions list has one entry per row, so `strict=None`, omitted `strict`, and `strict=True` produce the same entries.
- `harbor_research_runner.records.jsonl.x_bad_line_time_spans__mutmut_30`, `harbor_research_runner.records.jsonl.x_bad_line_time_spans__mutmut_33`: no entry has a position equal to a bad line, so changing `<` to `<=` or `>` to `>=` does not change either neighbor list.
- `harbor_research_runner.records.jsonl.x_bad_line_time_spans__mutmut_47`: `float` accepts `-INF` as the same negative infinity value as `-inf`.
- `harbor_research_runner.records.jsonl.x_bad_line_time_spans__mutmut_52`: `float` accepts `INF` as the same positive infinity value as `inf`.

### harbor_research_runner.evidence_bundle.verify

- `harbor_research_runner.evidence_bundle.verify.x__host_window_damaged__mutmut_58`: `zip(..., strict=None)` has the same non-strict behavior as `strict=False`.
- `harbor_research_runner.evidence_bundle.verify.x__host_window_damaged__mutmut_61`: omitting `strict` uses the same default non-strict behavior as `strict=False`.
- `harbor_research_runner.evidence_bundle.verify.x__json__mutmut_5`: Python's codec lookup treats `UTF-8` and `utf-8` as the same encoding.
- `harbor_research_runner.evidence_bundle.verify.x__jsonl_gzip__mutmut_13`: Python's codec lookup treats `UTF-8` and `utf-8` as the same encoding.
- `harbor_research_runner.evidence_bundle.verify.x__resource_rows__mutmut_19`: Python's codec lookup treats `UTF-8` and `utf-8` as the same encoding.
- `harbor_research_runner.evidence_bundle.verify.x_malformed_jsonl_count__mutmut_16`: Python's codec lookup treats `UTF-8` and `utf-8` as the same encoding.
- `harbor_research_runner.evidence_bundle.verify.x_verify__mutmut_83`: Python's codec lookup treats `UTF-8` and `utf-8` as the same encoding when reading the report.
- `harbor_research_runner.evidence_bundle.verify.x_verify__mutmut_15`: omitting the split limit does not change the first split field.
- `harbor_research_runner.evidence_bundle.verify.x_verify__mutmut_18`: a positive split limit of two does not change the first split field.

### harbor_research_runner.run.checks

- `x_run_checks__mutmut_3`: `machine_check_error` is read only when the machine check failed to load, and that path always reassigns it.
- `x__bind_port__mutmut_4`: omitting the socket type uses Python's default `SOCK_STREAM`.
- `x__bind_port__mutmut_11`: `SO_REUSEADDR` accepts any nonzero value, so `2` enables the same option as `1`.
- `x_run_checks__mutmut_104`: valid parsed jobs have a path only when `tasks.kind == "path"`, so changing `and` to `or` does not change the branch for valid jobs.

### harbor_research_runner.records.proxy_ledger

- `x_sequence_gaps__mutmut_3`: `strict=None` behaves as `zip`'s default non-strict mode.
- `x_sequence_gaps__mutmut_6`: omitting `strict=False` uses `zip`'s default non-strict behavior.

### harbor_research_runner.run.loop

- `x__attach_run_log__mutmut_6`: omitting `FileHandler`'s `mode` preserves its default append mode (`"a"`).
- `x__attach_run_log__mutmut_14`: Python's codec lookup treats `"UTF-8"` and `"utf-8"` as the same encoding.
- `x_run_job__mutmut_4`, `x_run_job__mutmut_195`, `x_run_job__mutmut_243`, `x_resume_run__mutmut_14`, `x_resume_run__mutmut_230`, `x_resume_run__mutmut_261`: each changes a `services_started` value from `False` to `None`; the value is only used as a truthiness guard and is set to `True` after services start, so both values select the same cleanup behavior.
- `x_run_job__mutmut_196`: the canary-failure branch has already stopped services, and the `finally` cleanup is idempotent, so retaining the started flag only repeats that cleanup without changing observable output.
- `x__task_ref__mutmut_{1,11,12}`: parsed `Job` values always carry exactly one complete task-source variant, so these alternate boolean expressions have the same result for every valid job.
- `x__task_ref__mutmut_6`: parsed `Job` values always carry exactly one complete task-source variant, so the alternate boolean expression has the same result for every valid job.
- `x_make_read_only__mutmut_{3,4,6,7,11}`: changing traversal sort options does not change the final file modes; chmod of child entries does not require parent write permission.
- `x_run_job__mutmut_6`: the initial `checks` value is replaced by `run_checks` before any branch reads it.
- `x_run_job__mutmut_44`: `RunSession.manifest_path` is retained as an unused attribute; agent settings receive the independent `manifest_path` argument.
- `x_run_job__mutmut_86`: `None` and `False` both select the non-restart path in `RunSession.start_services`.
- `x_resume_run__mutmut_140`: omitting `run_dir` from `run_checks` supplies the same default `None` as the explicit `run_dir=None` mutation `x_resume_run__mutmut_132`.
- `xǁRunSessionǁcreate__mutmut_26` and `xǁRunSessionǁcreate__mutmut_28`: `Path.mkdir` treats `exist_ok=None` and omitted `exist_ok` like `False`.

Message-text mutants:

### harbor_research_runner.evidence_bundle.trials

1 mutant changes only the separator in the `ValueError` text listing trials without verdict events.

### harbor_research_runner.evidence_bundle.build

6 mutants change only the digest-mismatch exception text, report whitespace, or verification log text.

### harbor_research_runner.evidence_bundle.verify

4: `x__json__mutmut_7`, `x__jsonl_gzip__mutmut_7`, `x__jsonl_gzip__mutmut_8` and `x__jsonl_gzip__mutmut_15`. These alter exception wording or a displayed JSONL line number only; no record, exit code or documented remedy depends on the wording.

### harbor_research_runner.run.checks

47 survivors change only human-readable check details; launch decisions do not depend on that wording. Where a mutant puts `None` into a detail list, the join raises inside `_evaluate`, which still records the check as failed.

### harbor_research_runner.records.proxy_ledger

14 survivors change only `ValueError` messages; callers and record validity depend on the exception type and parsed value, not its wording.

### harbor_research_runner.run.loop

47 survivors change only human-readable exception or log text; no record, return value or documented remedy depends on it: `x_make_read_only__mutmut_{28,31}`, `x__canary_passed__mutmut_{59,60}`, `x_resume_run__mutmut_{3,4,5,6,44,46,47,73,75,91,93,94,112,115,117,143,145,146,147,148,149,150,151,152,267,268,269}`, `x_run_job__mutmut_{191,253,255,259,266,267,268}`, `xǁRefusedToStartǁ__init____mutmut_1`, `xǁRunSessionǁcreate__mutmut_{64,68,69}` and `xǁRunSessionǁstart_services__mutmut_{160,161,172,173,174}`.

Not unit-testable:

### harbor_research_runner.evidence_bundle.trials

- `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_3` and `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_96`: distinguishing the default text encoding from UTF-8 requires changing the interpreter's locale encoding, which is not an injectable boundary for these functions.

### harbor_research_runner.evidence_bundle.build

- `harbor_research_runner.evidence_bundle.build.x_build__mutmut_133` and `harbor_research_runner.evidence_bundle.build.x_build__mutmut_135`: using the system temporary directory differs only when that directory and the requested output are on separate filesystems; this runner has no injectable filesystem boundary and the test host exposes no such deterministic mount.
- `harbor_research_runner.evidence_bundle.build.x_build__mutmut_168`, `harbor_research_runner.evidence_bundle.build.x_build__mutmut_169`, `harbor_research_runner.evidence_bundle.build.x_build__mutmut_170`, `harbor_research_runner.evidence_bundle.build.x_build__mutmut_171`, `harbor_research_runner.evidence_bundle.build.x_build__mutmut_172` and `harbor_research_runner.evidence_bundle.build.x_build__mutmut_173`: these affect only restoration after the temporary-directory rename fails; the public function has no filesystem-failure injection point, and inducing a rename race would make a unit test nondeterministic.
- `harbor_research_runner.evidence_bundle.build.x_build__mutmut_175`: this affects temporary-directory cleanup after an earlier filesystem exception, which cannot be induced through the public inputs without patching the filesystem boundary.

### harbor_research_runner.evidence_bundle.verify

- `harbor_research_runner.evidence_bundle.verify.x__json__mutmut_3`: the default text encoding differs from UTF-8 only under a non-UTF-8 locale, which is not an injectable boundary for `verify`.
- `harbor_research_runner.evidence_bundle.verify.x_verify__mutmut_78`: the default text encoding differs from UTF-8 only under a non-UTF-8 locale, which is not an injectable boundary for `verify`.

### harbor_research_runner.run.loop

- `x__attach_run_log__mutmut_{4,7}`: omitting the run log's explicit encoding falls back to the locale's encoding, which is UTF-8 on the test hosts; only a non-UTF-8 locale would show the difference.
- `x_sampler_command__mutmut_{2,6}` and `xǁRunSessionǁstart_services__mutmut_{76,111,115,140,144}`: these only uppercase the executable name (`uv` or `python`). On macOS's case-insensitive system disk, PATH lookup still finds the executable; Linux kills these mutants.
- `xǁRunSessionǁstart_services__mutmut_25`: upper-casing `request-bodies` only differs on a case-sensitive filesystem, which the current test host does not provide.
- `xǁRunSessionǁcreate__mutmut_16`: the local-time conversion differs from UTC only when the host timezone is non-UTC, and this public API has no timezone injection boundary.

`tests/integration/run/test_loop.py` starts the real proxy and sampler services, and mutmut runs integration tests. These remaining `RunSession.start_services` survivors require subprocess, file-descriptor, socket, or health-endpoint behavior and are not reachable through the unit or integration public boundaries: `xǁRunSessionǁstart_services__mutmut_{27,28,51,53,54,57,58,68,72,101,109,132,133,136,137,157,158,166,167}`.

### Covered only by the end-to-end lifecycle

The main pass, set-aside and resume-after-canary paths can't be reached without Docker and a real Harbor canary, and a follow-up issue tracks making them reachable.

- 134 survivors: `x__canary_passed__mutmut_{8,25,26,27,28,29,58,61,62,65,66,69,70,71}`, `x__pass_loop__mutmut_{2,16,17,18,19,21,22,23,24,25,26,30,31,38,39,42,43,48,51,57,59,70,71,72,78,79,83,84,85,86,91,92,94,95,96,100,101,102,107,109,110,111,118,124,125,140,148,162,163,164}`, `x__set_aside_trials__mutmut_{21,22}`, `x_resume_run__mutmut_{187,238,239,240,241,242,243,244,245,246,247,248,249,259,260,262,277,278,279}` and `x_run_job__mutmut_{174,182,183,202,203,204,205,206,207,208,209,210,211,212,213,214,215,216,217,218,219,220,221,222,223,224,225,226,227,228,229,230,231,232,233,234,235,236,237,238,239,240,241,242,244,245,246,247,248}`.
- `x_run_job__mutmut_{80,81,82}`: these deadline fields are consumed by the main pass, which starts only after the Harbor canary passes.

Simplification candidates:

### harbor_research_runner.evidence_bundle.build

- `harbor_research_runner.evidence_bundle.build.x_build__mutmut_176`: after a successful replace, `build` deletes the backup folder and the `finally` block deletes it again, so the `finally` deletion runs only on the error path. Removing the first deletion leaves one cleanup that the replace test covers.

### harbor_research_runner.trial_validity.verdict

- `harbor_research_runner.trial_validity.verdict.x__physical_positions__mutmut_2`, `harbor_research_runner.trial_validity.verdict.x__physical_positions__mutmut_3`, `harbor_research_runner.trial_validity.verdict.x__physical_positions__mutmut_4`, `harbor_research_runner.trial_validity.verdict.x__physical_positions__mutmut_5`, `harbor_research_runner.trial_validity.verdict.x__physical_positions__mutmut_15`, `harbor_research_runner.trial_validity.verdict.x__physical_positions__mutmut_19`, `harbor_research_runner.trial_validity.verdict.x__physical_positions__mutmut_20`, and `harbor_research_runner.trial_validity.verdict.x__view__mutmut_6`, `harbor_research_runner.trial_validity.verdict.x__view__mutmut_9`, `harbor_research_runner.trial_validity.verdict.x__view__mutmut_10`: both lists have the same length by construction. `zip(..., strict=True)` plus a plain range of positions would make the length mutants raise; the orchestrator will make that source change separately.
- `harbor_research_runner.trial_validity.verdict.x__measurement__mutmut_59`, `harbor_research_runner.trial_validity.verdict.x__measurement__mutmut_62`, and `harbor_research_runner.trial_validity.verdict.x__host_samples_damaged__mutmut_48`, `harbor_research_runner.trial_validity.verdict.x__host_samples_damaged__mutmut_51`: these change only `strict=False`; `itertools.pairwise` removes that argument.

### harbor_research_runner.evidence_bundle.verify

- `harbor_research_runner.evidence_bundle.verify.x__host_window_damaged__mutmut_73` and `x__host_window_damaged__mutmut_78`: the absent-resource fallback's `torn_last_line` value is unused; `bad_line_time_spans` only reads `rows` and `bad_lines`.
- `harbor_research_runner.evidence_bundle.verify.x_verify__mutmut_52`, `x_verify__mutmut_57`, `x_verify__mutmut_61` and `x_verify__mutmut_66`: the absent OOM and memory fallbacks' `torn_last_line` value is unused by telemetry span checks.
- `harbor_research_runner.evidence_bundle.verify.x_verify__mutmut_799` and `x_verify__mutmut_801`: an absent sample list defaults to either `None` or `[]`, and both are falsey before any `max` call.

### harbor_research_runner.run.checks

- `harbor_research_runner.run.checks.x_run_checks__mutmut_127` and `harbor_research_runner.run.checks.x_run_checks__mutmut_128`: the `except OSError` around `task_content_hash` is unreachable for missing paths, regular files and folders with unreadable files, because Harbor's `Packager.compute_content_hash` returns the empty-content digest for all three; those tasks are already reported as drifted by the digest comparison.
- `harbor_research_runner.run.checks.x_run_checks__mutmut_235`: task memory is either returned through the earlier missing-memory branch or `max_task_memory` includes the canary limit, so this `None` branch is unreachable.
- `harbor_research_runner.run.checks.x_host_resources_check__mutmut_4`, `x_machine_check_launch_checks__mutmut_29` and `_35`, `x_run_checks__mutmut_176`, `_180`, `_310` and `_316`: they change the verifier timeout multiplier passed into `Limits`, a field `host_breach` never reads.

### harbor_research_runner.run.loop

- `x__task_ref__mutmut_{18,19,20}`: these change only the private helper's error wording for an incomplete `Job`; the public job parser refuses incomplete task sources before this helper is reached.


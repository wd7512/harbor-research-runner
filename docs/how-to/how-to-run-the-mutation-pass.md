# Mutation pass

Run the pass with `uv run scripts/mutation_pass.py`. It runs mutmut against the configured unit and integration tests and writes per-mutant results to `.agent/mutation-results.json`.

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

The wrapper reads mutmut's per-mutant exit codes and counts only 1 as killed; 0 is survived, and every other code is unverified. The wrapper's pytest report hook also marks a mutant unverified if any test reports that it exceeded the unit CPU budget. The 0.1 second budget remains unchanged.

The source-inspection tests are excluded: `tests/unit/test_cli.py::test_cli_does_not_read_environment_variables_by_name`, `tests/unit/test_import_rule.py` and `tests/integration/test_import_rule.py`. A new test that reads source as text must be added to `pytest_add_cli_args_test_selection` in `pyproject.toml`. Mutmut 3.7.0 restricts files but not named functions, so it mutates all of `run/loop.py` and `run/checks.py`.

Run the pass on a case-sensitive filesystem: Linux, or on macOS a case-sensitive APFS disk image with `TMPDIR` pointed inside it. On a case-insensitive disk, a mutant that only upper-cases a file name (`'run-events.jsonl'` to `'RUN-EVENTS.JSONL'`) still opens the same file and survives. The image needs at least 20 GB free, because the host-resources check reads the free space under the test's temporary folder. When running `mutmut run` directly instead of through the wrapper, set `UV_PROJECT_ENVIRONMENT` to the repository's `.venv` as the wrapper does; otherwise the first test that runs `uv` inside `mutants/` builds a new environment and exceeds its time budget.

For each survivor, write a test that detects the behavior through the public API, or list it below with its reason. A survivor that only a private helper, a private attribute or a patch could kill is a simplification candidate: it marks code the public behaviour does not need.

Equivalent mutants:

### harbor_research_runner.evidence_bundle.trials

- `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_8` and `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_101`: the `UTF-8` codec name is case-insensitive, so both read the same UTF-8 bytes.
- `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_53`: the changed guard only adds memory-map entries with a non-run location or a non-string trial. Those keys are never looked up when constructing a trial row, so no returned row changes.
- `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_93`: Python parses `-INF` and `-inf` as the same negative infinity.
- `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_424` and `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_478`: Python parses `INF` and `inf` as the same positive infinity.

### harbor_research_runner.evidence_bundle.build

- `harbor_research_runner.evidence_bundle.build.x__gzip__mutmut_4`: `filename=None` and `filename=""` produce the same gzip header when the file object is an unnamed `BytesIO`.
- `harbor_research_runner.evidence_bundle.build.x__gzip__mutmut_9`: omitting `mode` lets `GzipFile` infer its write mode from the supplied file object; the compressed payload is unchanged.
- `harbor_research_runner.evidence_bundle.build.x__gzip__mutmut_10`, `harbor_research_runner.evidence_bundle.build.x__gzip__mutmut_11` and `harbor_research_runner.evidence_bundle.build.x__gzip__mutmut_15`: these select a different valid compression level, which changes only the gzip representation and not its decompressed content or required `mtime`.
- `harbor_research_runner.evidence_bundle.build.x__gzip__mutmut_14`: a constant gzip filename changes optional header metadata but not the decompressed request rows or deterministic output for the same input.
- `harbor_research_runner.evidence_bundle.build.x__planned_slots__mutmut_7`, `harbor_research_runner.evidence_bundle.build.x__planned_slots__mutmut_9`, `harbor_research_runner.evidence_bundle.build.x__planned_slots__mutmut_11` and `harbor_research_runner.evidence_bundle.build.x__planned_slots__mutmut_13`: the alternatives differ only for absent or null task-set fields; valid run metadata requires `task_set.tasks` to be a list.
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

Message-text mutants:

### harbor_research_runner.evidence_bundle.trials

1 mutant changes only the separator in the `ValueError` text listing trials without verdict events.

### harbor_research_runner.evidence_bundle.build

6 mutants change only the digest-mismatch exception text, report whitespace, or verification log text.

### harbor_research_runner.evidence_bundle.verify

8: `x__json__mutmut_7`, `x__jsonl_gzip__mutmut_7`, `x__jsonl_gzip__mutmut_8`, `x__jsonl_gzip__mutmut_15`, `x_verify__mutmut_942`, `x_verify__mutmut_943`, `x_verify__mutmut_944`, `x_verify__mutmut_945`. These alter exception wording or a displayed JSONL line number only; no record, exit code or documented remedy depends on the wording.

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

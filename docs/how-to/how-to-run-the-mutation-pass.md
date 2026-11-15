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

For each survivor, write a test that detects the behavior or add its mutant name and equivalent-mutant rationale to the list below.

### `trial_validity.verdict`

- `harbor_research_runner.trial_validity.verdict.x__measurement__mutmut_19`: `damaged` starts as `None` instead of `False`; it is only read as a condition, where both are false.
- `harbor_research_runner.trial_validity.verdict.x__position_within__mutmut_2`, `harbor_research_runner.trial_validity.verdict.x__position_within__mutmut_3`, `harbor_research_runner.trial_validity.verdict.x__position_within__mutmut_4`: an invalid row at the open or close position is the trial's own row, and the trial-row check already marks it damaged.
- `harbor_research_runner.trial_validity.verdict.x__positive_seq__mutmut_8`: sequence zero is rejected by `records.proxy_ledger.is_valid_row`; the invalid trial row already marks measurement damaged, so treating its sequence as positive cannot change the public verdict.
- `harbor_research_runner.trial_validity.verdict.x_judge_trial__mutmut_159`, `harbor_research_runner.trial_validity.verdict.x_judge_trial__mutmut_217`: omitting the explicit `None` for `oom_source` uses the `Verdict` field's default `None`.
- `harbor_research_runner.trial_validity.verdict.x_judge_trial__mutmut_220`: in the repeated-setup branch the phase is never `agent` or `verifier`, the only values `_labels` reads, so passing `None` gives the same labels.
- `harbor_research_runner.trial_validity.verdict.x_judge_trial__mutmut_223`, `harbor_research_runner.trial_validity.verdict.x_judge_trial__mutmut_288`: a `False` flag passed to `_labels` becomes `None`; `_labels` only reads it as a condition.

### `trial_validity.verdict` simplification candidates

- `harbor_research_runner.trial_validity.verdict.x__physical_positions__mutmut_2`, `harbor_research_runner.trial_validity.verdict.x__physical_positions__mutmut_3`, `harbor_research_runner.trial_validity.verdict.x__physical_positions__mutmut_4`, `harbor_research_runner.trial_validity.verdict.x__physical_positions__mutmut_5`, `harbor_research_runner.trial_validity.verdict.x__physical_positions__mutmut_15`, `harbor_research_runner.trial_validity.verdict.x__physical_positions__mutmut_19`, `harbor_research_runner.trial_validity.verdict.x__physical_positions__mutmut_20`, and `harbor_research_runner.trial_validity.verdict.x__view__mutmut_6`, `harbor_research_runner.trial_validity.verdict.x__view__mutmut_9`, `harbor_research_runner.trial_validity.verdict.x__view__mutmut_10`: both lists have the same length by construction. `zip(..., strict=True)` plus a plain range of positions would make the length mutants raise; the orchestrator will make that source change separately.
- `harbor_research_runner.trial_validity.verdict.x__measurement__mutmut_59`, `harbor_research_runner.trial_validity.verdict.x__measurement__mutmut_62`, and `harbor_research_runner.trial_validity.verdict.x__host_samples_damaged__mutmut_48`, `harbor_research_runner.trial_validity.verdict.x__host_samples_damaged__mutmut_51`: these change only `strict=False`; `itertools.pairwise` removes that argument.

### `records.jsonl`

- `harbor_research_runner.records.jsonl.x_bad_line_time_spans__mutmut_15`: the extra position is truncated by the zip against the rows.
- `harbor_research_runner.records.jsonl.x_bad_line_time_spans__mutmut_21`, `harbor_research_runner.records.jsonl.x_bad_line_time_spans__mutmut_24`, `harbor_research_runner.records.jsonl.x_bad_line_time_spans__mutmut_25`: the positions list has one entry per row, so `strict=None`, omitted `strict`, and `strict=True` produce the same entries.
- `harbor_research_runner.records.jsonl.x_bad_line_time_spans__mutmut_30`, `harbor_research_runner.records.jsonl.x_bad_line_time_spans__mutmut_33`: no entry has a position equal to a bad line, so changing `<` to `<=` or `>` to `>=` does not change either neighbor list.
- `harbor_research_runner.records.jsonl.x_bad_line_time_spans__mutmut_47`: `float` accepts `-INF` as the same negative infinity value as `-inf`.
- `harbor_research_runner.records.jsonl.x_bad_line_time_spans__mutmut_52`: `float` accepts `INF` as the same positive infinity value as `inf`.

### Message-text mutants

None recorded.

### Not unit-testable

None recorded.

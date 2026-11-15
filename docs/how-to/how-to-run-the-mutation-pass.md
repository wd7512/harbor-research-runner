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

- `harbor_research_runner.trial_validity.verdict.x__physical_positions__mutmut_20`: the changed `continue` is reached only for the final torn line, so `break` also ends the loop without changing the result.
- `harbor_research_runner.trial_validity.verdict.x__view__mutmut_6`, `harbor_research_runner.trial_validity.verdict.x__view__mutmut_9`, `harbor_research_runner.trial_validity.verdict.x__view__mutmut_10`: the positions list always has one entry per row, so `strict=None`, omitted `strict`, and `strict=True` produce the same view.
- `harbor_research_runner.trial_validity.verdict.x__measurement__mutmut_19`, `harbor_research_runner.trial_validity.verdict.x__measurement__mutmut_23`, `harbor_research_runner.trial_validity.verdict.x__measurement__mutmut_24`: a damaged trial row is also present in `ledger_problems.rows`, where the later view check detects it; valid runner inputs cannot make the initial flag the only evidence of damage.
- `harbor_research_runner.trial_validity.verdict.x__measurement__mutmut_25`, `harbor_research_runner.trial_validity.verdict.x__measurement__mutmut_39`: the alternate condition only changes whether the damage scan runs after damage is already known; it cannot clear the damaged result.
- `harbor_research_runner.trial_validity.verdict.x__measurement__mutmut_59`, `harbor_research_runner.trial_validity.verdict.x__measurement__mutmut_62`: the first sequence list and its one-element-shorter tail are intentionally paired with non-strict zip; `None` and omitted `strict` have the same behavior.
- `harbor_research_runner.trial_validity.verdict.x__host_samples_damaged__mutmut_48`, `harbor_research_runner.trial_validity.verdict.x__host_samples_damaged__mutmut_51`: the timestamp list and its one-element-shorter tail are intentionally paired with non-strict zip; `None` and omitted `strict` have the same behavior.
- `harbor_research_runner.trial_validity.verdict.x_judge_trial__mutmut_159`, `harbor_research_runner.trial_validity.verdict.x_judge_trial__mutmut_217`: omitting the explicit `None` for `oom_source` uses the `Verdict` field's default `None`.

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

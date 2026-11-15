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

## Equivalent mutants

### harbor_research_runner.evidence_bundle.verify

- `harbor_research_runner.evidence_bundle.verify.x__host_window_damaged__mutmut_58`: `zip(..., strict=None)` has the same non-strict behavior as `strict=False`.
- `harbor_research_runner.evidence_bundle.verify.x__host_window_damaged__mutmut_61`: omitting `strict` uses the same default non-strict behavior as `strict=False`.
- `harbor_research_runner.evidence_bundle.verify.x__json__mutmut_5`: Python's codec lookup treats `UTF-8` and `utf-8` as the same encoding.
- `harbor_research_runner.evidence_bundle.verify.x__jsonl_gzip__mutmut_13`: Python's codec lookup treats `UTF-8` and `utf-8` as the same encoding.
- `harbor_research_runner.evidence_bundle.verify.x__resource_rows__mutmut_19`: Python's codec lookup treats `UTF-8` and `utf-8` as the same encoding.
- `harbor_research_runner.evidence_bundle.verify.x_malformed_jsonl_count__mutmut_16`: Python's codec lookup treats `UTF-8` and `utf-8` as the same encoding.

## Message-text mutants

### harbor_research_runner.evidence_bundle.verify

7: `x__json__mutmut_7`, `x__jsonl_gzip__mutmut_8`, `x__jsonl_gzip__mutmut_15`, `x_verify__mutmut_942`, `x_verify__mutmut_943`, `x_verify__mutmut_944`, `x_verify__mutmut_945`. These alter exception wording or a displayed JSONL line number only; no record, exit code or documented remedy depends on the wording.

## Not unit-testable

None. The verifier operates on bundle files and its surviving paths can be exercised with the existing fixture and bundle helpers.

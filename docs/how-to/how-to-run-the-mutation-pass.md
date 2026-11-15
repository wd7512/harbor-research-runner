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

- `harbor_research_runner.evidence_bundle.verify.x__host_window_damaged__mutmut_9`: a valid trial record has named string keys, so looking up the `None` key cannot differ from the intended `verifier_started_t` lookup.
- `harbor_research_runner.evidence_bundle.verify.x__host_window_damaged__mutmut_58`: `zip(..., strict=None)` has the same non-strict behavior as `strict=False`.
- `harbor_research_runner.evidence_bundle.verify.x__host_window_damaged__mutmut_61`: omitting `strict` uses the same default non-strict behavior as `strict=False`.

## Message-text mutants

### harbor_research_runner.evidence_bundle.verify

0 among the 32 survivors in the partial lane results.

## Not unit-testable

None among the 32 survivors in the partial lane results.

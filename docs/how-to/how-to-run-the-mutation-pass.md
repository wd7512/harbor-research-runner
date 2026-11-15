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

Equivalent mutants:

### harbor_research_runner.run.checks

- `x__bind_port__mutmut_4`: omitting the socket type uses Python's default `SOCK_STREAM`.
- `x__bind_port__mutmut_11`: `SO_REUSEADDR` accepts any nonzero value, so `2` enables the same option as `1`.
- `x_host_resources_check__mutmut_4`, `x_machine_check_launch_checks__mutmut_29`, `x_machine_check_launch_checks__mutmut_35`, `x_run_checks__mutmut_176`, `x_run_checks__mutmut_180`, `x_run_checks__mutmut_310` and `x_run_checks__mutmut_316`: the changed verifier timeout multiplier only fills `Limits` fields that `host_breach` does not read.
- `x_run_checks__mutmut_104`: valid parsed jobs have a path only when `tasks.kind == "path"`, so changing `and` to `or` does not change the branch for valid jobs.
- `x_run_checks__mutmut_235`: task memory is either returned through the earlier missing-memory branch or `max_task_memory` includes the canary limit, so this `None` branch is unreachable.

### harbor_research_runner.records.proxy_ledger

- `x_sequence_gaps__mutmut_6`: omitting `strict=False` uses `zip`'s default non-strict behavior.

Message-text mutants:

### harbor_research_runner.run.checks

52 survivors change only human-readable check details; launch decisions do not depend on that wording.

### harbor_research_runner.records.proxy_ledger

15 survivors change only `ValueError` messages; callers and record validity depend on the exception type and parsed value, not its wording.

Not unit-testable: none recorded.

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

### harbor_research_runner.run.loop

- `x__attach_run_log__mutmut_6`: omitting `FileHandler`'s `mode` preserves its default append mode (`"a"`).
- `x__attach_run_log__mutmut_14`: Python's codec lookup treats `"UTF-8"` and `"utf-8"` as the same encoding.
- `x_run_job__mutmut_4`, `x_run_job__mutmut_195`, `x_run_job__mutmut_243`, `x_resume_run__mutmut_14`, `x_resume_run__mutmut_230`, `x_resume_run__mutmut_261`: each changes a `services_started` value from `False` to `None`; the value is only used as a truthiness guard and is set to `True` after services start, so both values select the same cleanup behavior.

Message-text mutants:

### harbor_research_runner.evidence_bundle.trials

1 mutant changes only the separator in the `ValueError` text listing trials without verdict events.

### harbor_research_runner.evidence_bundle.build

6 mutants change only the digest-mismatch exception text, report whitespace, or verification log text.

### harbor_research_runner.run.loop

47 survivors change only human-readable exception or log text. They do not alter records, return values, or a documented remedy.

Not unit-testable:

### harbor_research_runner.evidence_bundle.trials

- `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_3` and `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_96`: distinguishing the default text encoding from UTF-8 requires changing the interpreter's locale encoding, which is not an injectable boundary for these functions.

### harbor_research_runner.evidence_bundle.build

- `harbor_research_runner.evidence_bundle.build.x_build__mutmut_133` and `harbor_research_runner.evidence_bundle.build.x_build__mutmut_135`: using the system temporary directory differs only when that directory and the requested output are on separate filesystems; this runner has no injectable filesystem boundary and the test host exposes no such deterministic mount.
- `harbor_research_runner.evidence_bundle.build.x_build__mutmut_168`, `harbor_research_runner.evidence_bundle.build.x_build__mutmut_169`, `harbor_research_runner.evidence_bundle.build.x_build__mutmut_170`, `harbor_research_runner.evidence_bundle.build.x_build__mutmut_171`, `harbor_research_runner.evidence_bundle.build.x_build__mutmut_172` and `harbor_research_runner.evidence_bundle.build.x_build__mutmut_173`: these affect only restoration after the temporary-directory rename fails; the public function has no filesystem-failure injection point, and inducing a rename race would make a unit test nondeterministic.
- `harbor_research_runner.evidence_bundle.build.x_build__mutmut_175`: this affects temporary-directory cleanup after an earlier filesystem exception, which cannot be induced through the public inputs without patching the filesystem boundary.

### harbor_research_runner.run.loop

`RunSession.start_services` has no injected process, socket, or clock dependencies. Its survivors require spawning the proxy/watch processes, passing file descriptors, binding a loopback socket, or waiting on the health endpoint, which unit and integration tests prohibit or would only exercise through those fakes. The following 71 mutants are therefore reserved for the Docker end-to-end tier:

- `xǁRunSessionǁstart_services__mutmut_{5,8,9,47,51,53,54,57,58,59,64,66,68,69,72,73,74,76,77,78,79,80,81,82,83,84,85,86,87,89,91,92,93,99,101,103,104,107,108,109,111,112,113,114,115,116,117,118,119,120,121,122,123,124,125,126,127,130,132,133,136,137,138,140,144,157,158,164,166,167,171}`.

Simplification candidates:

### harbor_research_runner.evidence_bundle.build

- `harbor_research_runner.evidence_bundle.build.x_build__mutmut_176`: after a successful replace, `build` deletes the backup folder and the `finally` block deletes it again, so the `finally` deletion runs only on the error path. Removing the first deletion leaves one cleanup that the replace test covers.

### harbor_research_runner.run.loop

These mutations are reachable only through private helpers or active lifecycle state that public entry points cannot supply with the permitted fakes:

- `x__deadline_timestamp__mutmut_2`: its internal timestamp is observable only after a pass loop begins with lifecycle state unavailable to the public fake setup.
- `xǁRunSessionǁstop_services__mutmut_21`: active sampler cleanup requires private process state installed by `start_services`.
- `xǁRunSessionǁstop_services__mutmut_30`: active proxy cleanup requires private process state installed by `start_services`.
- `x__pass_loop__mutmut_2`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_4`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_5`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_10`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_11`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_16`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_17`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_18`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_19`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_21`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_22`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_23`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_24`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_25`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_30`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_31`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_32`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_34`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_35`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_36`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_37`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_38`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_40`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_41`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_42`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_43`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_44`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_45`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_46`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_47`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_48`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_51`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_52`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_57`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_59`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_70`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_71`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_72`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_78`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_83`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_84`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_85`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_86`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_91`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_92`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_94`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_95`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_96`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_100`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_101`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_102`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_107`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_109`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_111`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_118`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_124`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_140`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_148`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_162`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_163`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.
- `x__pass_loop__mutmut_164`: this branch needs pass-loop and lifeline state unavailable through the public fake setup.

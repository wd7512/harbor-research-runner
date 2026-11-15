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

Run-loop coverage added for the case-sensitive pass (expected kills; mutmut was not rerun):

- `test_run_job_records_nonzero_canary_exit_and_stops_services` checks `_agent_configs` and `_arm_rows`, expected `x__agent_configs__mutmut_{2,4,13,22,23}` and `x__arm_rows__mutmut_{2,3}`.
- `test_run_job_records_path_and_git_task_references` checks task references through `run_job`, expected `x__task_ref__mutmut_{7,8,9,10,13,14,15,16,17}`.
- The slot tests check unrelated event filtering, exempt set-asides and cap boundaries, expected `x_slots_status__mutmut_{4,20,21,22,31,43,72,81,82}`.
- The nested read-only test checks symlink traversal and preserved modes, expected `x_make_read_only__mutmut_{12,17,36}`.
- The creation and load assertions check recorded identity and resume refusal outcomes, expected `xǁRunSessionǁcreate__mutmut_{43,76,132,140}`, `xǁRunSessionǁload__mutmut_25` and `x_resume_run__mutmut_{8,9,10,11}`.
- The set-aside recovery test reaches repair through `resume_run`, expected `x_resume_run__mutmut_{172,173}`; its observable stop marker and the finish timestamp assertion expect `x__finish__mutmut_{1,9}`.

These assertions target 41 of the 306 original survivors. The six exit-33 liveness cases now have public-method coverage; the exit -24 timeout is counted as killed.

Expected survivor disposition after those 41 test kills: 265 total = 20 equivalent + 89 message-text + 152 end-to-end-only + 4 simplification candidates.

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
- `x_run_job__mutmut_196`: the canary-failure branch has already stopped services, and the `finally` cleanup is idempotent, so retaining the started flag only repeats that cleanup without changing observable output.
- `x__attach_run_log__mutmut_{4,7}`: the local runtime's default text encoding is UTF-8, matching the explicit encoding or its omission.
- `x__task_ref__mutmut_{1,11,12}`: parsed `Job` values always carry exactly one complete task-source variant, so these alternate boolean expressions have the same result for every valid job.
- `x_make_read_only__mutmut_{3,4,6,7,11}`: changing traversal sort options does not change the final file modes; chmod of child entries does not require parent write permission.

Message-text mutants:

### harbor_research_runner.evidence_bundle.trials

1 mutant changes only the separator in the `ValueError` text listing trials without verdict events.

### harbor_research_runner.evidence_bundle.build

6 mutants change only the digest-mismatch exception text, report whitespace, or verification log text.

### harbor_research_runner.run.loop

42 additional survivors in the case-sensitive pass change only human-readable exception or log text. Together with the 47 already recorded, 89 run-loop mutants change no record, return value, or documented remedy: `x_make_read_only__mutmut_{28,31}`, `x_resume_run__mutmut_{2,3,4,5,6,7,44,46,47,73,75,91,93,94,112,115,117,145,146,147,148,149,150,151,152,267,268,269}`, `x_run_job__mutmut_{187,191,253,255,259,266,267,268}`, `xǁRefusedToStartǁ__init____mutmut_1` and `xǁRunSessionǁcreate__mutmut_{64,68,69}`.

Not unit-testable:

### harbor_research_runner.evidence_bundle.trials

- `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_3` and `harbor_research_runner.evidence_bundle.trials.x_build_trial_rows__mutmut_96`: distinguishing the default text encoding from UTF-8 requires changing the interpreter's locale encoding, which is not an injectable boundary for these functions.

### harbor_research_runner.evidence_bundle.build

- `harbor_research_runner.evidence_bundle.build.x_build__mutmut_133` and `harbor_research_runner.evidence_bundle.build.x_build__mutmut_135`: using the system temporary directory differs only when that directory and the requested output are on separate filesystems; this runner has no injectable filesystem boundary and the test host exposes no such deterministic mount.
- `harbor_research_runner.evidence_bundle.build.x_build__mutmut_168`, `harbor_research_runner.evidence_bundle.build.x_build__mutmut_169`, `harbor_research_runner.evidence_bundle.build.x_build__mutmut_170`, `harbor_research_runner.evidence_bundle.build.x_build__mutmut_171`, `harbor_research_runner.evidence_bundle.build.x_build__mutmut_172` and `harbor_research_runner.evidence_bundle.build.x_build__mutmut_173`: these affect only restoration after the temporary-directory rename fails; the public function has no filesystem-failure injection point, and inducing a rename race would make a unit test nondeterministic.
- `harbor_research_runner.evidence_bundle.build.x_build__mutmut_175`: this affects temporary-directory cleanup after an earlier filesystem exception, which cannot be induced through the public inputs without patching the filesystem boundary.

### harbor_research_runner.run.loop

`tests/integration/run/test_loop.py` starts the real proxy and sampler services, and mutmut runs integration tests. The remaining `RunSession.start_services` survivors require process, file-descriptor, socket, or health-endpoint behavior that is covered by the Docker end-to-end lifecycle tier:

- `xǁRunSessionǁstart_services__mutmut_{57,68,69,72,73,74,81,83,84,85,87}`.
- `x_run_job__mutmut_{174,182,183,202,203,204,205,206,207,208,209,210,211,212,213,214,215,216,217,218,219,220,221,222,223,224,225,226,227,228,229,230,231,232,233,234,235,236,237,238,239,240,241,242,244,245,246,247,248}`: these paths require a Harbor canary trial or a passing canary, and are covered by the e2e lifecycle scenarios, which mutmut does not run.
- `x__canary_passed__mutmut_{3,61,62,65,69,70,71}` and `x__pass_loop__mutmut_{26,39,79,110,125}`: these paths also require a successful Harbor canary and are covered by e2e lifecycle scenarios.
- `x__set_aside_trials__mutmut_{21,22}`: this helper runs in the main pass after a successful canary; mutation tests need the e2e lifecycle path.
- `x__pass_loop__mutmut_{2,4,5,10,11,16,17,18,19,21,22,23,24,25,30,31,32,34,35,36,37,38,40,41,42,43,44,45,46,47,48,51,57,59,70,71,72,78,83,84,85,86,91,92,94,95,96,100,101,102,107,109,111,118,124,140,148,162,163,164}`: the public run path reaches these changes only after a passing Harbor canary.
- `x_resume_run__mutmut_{187,238,239,240,241,242,243,244,245,246,247,248,249,259,260,262,277,278,279}`: these paths resume main work after a passing canary and are covered by the e2e lifecycle scenarios, which mutmut does not run.

Simplification candidates:

### harbor_research_runner.evidence_bundle.build

- `harbor_research_runner.evidence_bundle.build.x_build__mutmut_176`: after a successful replace, `build` deletes the backup folder and the `finally` block deletes it again, so the `finally` deletion runs only on the error path. Removing the first deletion leaves one cleanup that the replace test covers.

### harbor_research_runner.run.loop

`_deadline_timestamp` is private state setup; no public outcome or record depends on its internal epoch value.
- `x__task_ref__mutmut_{18,19,20}`: these change only the private helper's error wording for an incomplete `Job`; the public job parser refuses incomplete task sources before this helper is reached.

- `x__deadline_timestamp__mutmut_2`: its internal timestamp is observable only after a pass loop begins with lifecycle state unavailable to the public fake setup.

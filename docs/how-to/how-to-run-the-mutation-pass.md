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

### harbor_research_runner.run.loop

- `x__attach_run_log__mutmut_6`: omitting `FileHandler`'s `mode` preserves its default append mode (`"a"`).
- `x__attach_run_log__mutmut_14`: Python's codec lookup treats `"UTF-8"` and `"utf-8"` as the same encoding.
- `x_run_job__mutmut_4`, `x_run_job__mutmut_195`, `x_run_job__mutmut_243`, `x_resume_run__mutmut_14`, `x_resume_run__mutmut_230`, `x_resume_run__mutmut_261`: each changes a `services_started` value from `False` to `None`; the value is only used as a truthiness guard and is set to `True` after services start, so both values select the same cleanup behavior.

Message-text mutants:

### harbor_research_runner.run.loop

47 survivors change only human-readable exception or log text. They do not alter records, return values, or a documented remedy.

Not unit-testable:

### harbor_research_runner.run.loop

`RunSession.start_services` has no injected process, socket, or clock dependencies. Its survivors require spawning the proxy/watch processes, passing file descriptors, binding a loopback socket, or waiting on the health endpoint, which unit and integration tests prohibit or would only exercise through those fakes. The following 71 mutants are therefore reserved for the Docker end-to-end tier:

- `xǁRunSessionǁstart_services__mutmut_{5,8,9,47,51,53,54,57,58,59,64,66,68,69,72,73,74,76,77,78,79,80,81,82,83,84,85,86,87,89,91,92,93,99,101,103,104,107,108,109,111,112,113,114,115,116,117,118,119,120,121,122,123,124,125,126,127,130,132,133,136,137,138,140,144,157,158,164,166,167,171}`.

Simplification candidates (class 5):

### harbor_research_runner.run.loop

These mutations are reachable only through private helpers or active lifecycle state that public entry points cannot supply with the permitted fakes:

- `x__deadline_timestamp__mutmut_2`: its internal timestamp is observable only after a pass loop begins with lifecycle state unavailable to the public fake setup.
- `xǁRunSessionǁstop_services__mutmut_21`, `xǁRunSessionǁstop_services__mutmut_30`: active process cleanup requires private process state installed by `start_services`.
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

Case-only file names:

These mutations only change the case of a file or folder name and are checked by the orchestrator on a case-sensitive volume:

- `x__append_event__mutmut_5` (`run-events.jsonl`)
- `x__attach_run_log__mutmut_10` (`runner.log`)
- `x__current_slots_status__mutmut_5` (`run-events.jsonl`)
- `x__current_slots_status__mutmut_10` (`harbor-job`)
- `x__set_aside_trials__mutmut_5`, `x__set_aside_trials__mutmut_19` (`run-events.jsonl`)
- `x_resume_run__mutmut_33`, `x_resume_run__mutmut_170` (`run-events.jsonl`)
- `x_resume_run__mutmut_181` (`harbor-job`)
- `x_run_job__mutmut_119` (compose-host config)
- `x_run_job__mutmut_126` (canary-job config)
- `x_run_job__mutmut_137` (`CANARY-JOB` directory)
- `x_run_job__mutmut_221`, `x_run_job__mutmut_228` (main-job config)
- `xǁRunSessionǁcreate__mutmut_117` (machine-check file)
- `xǁRunSessionǁcreate__mutmut_124`, `xǁRunSessionǁload__mutmut_5` (run-metadata file)
- `xǁRunSessionǁstart_services__mutmut_90` (docker oom log)
- `x__pass_loop__mutmut_{26,39,79,110,125}` (`harbor-job` directory)
- `x__trials_left__mutmut_4` (`harbor-job` directory)

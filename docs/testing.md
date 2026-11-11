# Testing guide and worker rules

This file is the contributor guide. The design it serves is in `architecture.md`; the rulings behind it are in `decisions.md`.

## The four tiers

| Tier | Folder | What runs | Budget per test | Runs when |
|---|---|---|---|---|
| Unit | `tests/unit/` | One function or class, called directly with real inputs | 0.1 s CPU | Every gate |
| Integration | `tests/integration/` | The real proxy process against the scripted fake upstream; or two of our modules together through files | 5 s wall | Every gate |
| End-to-end | `tests/e2e/` | A real `harbor run` in Docker on the bundled tiny task, with the fake harness and the fake upstream | 5 min wall | CI on every PR; the orchestrator before merge |
| Live | `tests/live/` | Real OpenCode, a real model, one real task; the oracle run | none | Before a release, by the owner |

Contract tests are end-to-end tests marked `@pytest.mark.contract`. Each pins one Harbor behaviour we rely on. Run them alone with `uv run pytest tests/e2e -m contract` after any Harbor upgrade.

The gate is `uv run gate` (format, lint, types, unit and integration tests, lock file). End-to-end is `uv run gate --only e2e`. Never pipe a gate through `tail` or `head`; quote its exit code.

## Rules for every test

1. **Assert on outputs.** Call the public function, or run the command, and check the return value or the files on disk. Do not assert on which internal functions were called.
2. **Fakes exist at three boundaries only:** the upstream (`tests/support/fake_upstream.py`), the harness (`harness/fake.py`) and the clock (pass a `now` function or a timestamp argument). Do not patch, mock or monkeypatch any `harbor_research_runner` module.
3. **Unit tests do not sleep, start processes, or open sockets.** If a test needs one of those, it is an integration test.
4. **Use real shapes.** A test of a parser uses a real sample of what it parses, from `tests/fixtures/`. A test of a rule uses rows in the exact record format from `architecture.md`.
5. **Red first.** Write the test, run it, and see it fail for the reason you expect, before writing the code. Put the failing output in your report.
6. **One behaviour per test, named for the behaviour.** `test_trial_with_one_tolerated_error_is_valid`, not `test_judge_3`. No issue numbers in test names or comments.
7. **Carry old cases.** When a brief names an OLD test file, port its inputs and expected outputs as a table first. If an old case cannot apply because the behaviour was dropped, list it under "not carried" in your report with the reason.
8. **No new fixtures over 200 KB**, and no file in `tests/fixtures/` without a line in `tests/fixtures/README.md` saying where it came from.

## Rules for every worker

1. Work only in your worktree. One step, one commit. Do not push, and do not use `gh`.
2. Read `architecture.md` and your step brief before anything else. If they disagree, or the brief is wrong about OLD code, stop and report.
3. Make no design decisions. A missing detail is a reason to stop and report, not to choose.
4. Do not add a dependency, a command-line flag, an environment variable, a file format field or a module that the brief does not name.
5. Do not touch a module the brief does not name. If your change seems to need it, stop and report.
6. No comments in code unless the brief asks. Docstrings on public functions are fine. Log with `logging`; never `print` in the package.
7. Add one changelog fragment: `changes/<step number>.<feature|fix|doc|misc>.md`, one or two sentences.
8. Finish by running the brief's acceptance command and `uv run gate`, unpiped. Report both exit codes and the red-first output.

## How to write a test at each tier

**Unit.** Import the function. Build its inputs as plain values or rows. Compare the result. For anything involving time, pass the time in.

**Integration (proxy).** Use the `proxy` fixture in `tests/integration/conftest.py`: it starts the fake upstream with a script, writes a `proxy-config.json`, starts the real proxy process, and yields its URL and ledger path. Send requests with `urllib`. Assert on the ledger rows. The fake upstream script is a list of behaviours, one per request, for example `["ok", "http_502", "stall:30", "truncate_after:3", "ok"]`.

**End-to-end.** Use the `e2e_run` fixture in `tests/e2e/conftest.py`: it writes a job file with the fake harness on the bundled task `canary/say-hello`, runs `hrr run`, and yields the run folder. The fake harness reads its behaviour from its arm settings (`requests: 3`, `ignore_stop: true`, `exit_code: 1`, `sleep_s: 120`). To interrupt a run, the fixture offers `kill_runner()`, `kill_proxy()` and `resume()`. Assert on `run-events.jsonl`, `proxy-ledger.jsonl`, the folders, and the exit code.

## How to turn an incident into a test

1. Write down the shape in one sentence: what happened on disk or on the wire, not what the code did.
2. Pick the lowest tier that can produce that shape. A wrong classification is a unit test. Something the upstream did is an integration test. Anything involving Harbor's resume, Docker, or a killed process is end-to-end.
3. Tell the maintainer the shape, the tier and the test name, so the incident list stays complete.
4. Write the test so that it fails without the guard. Show the failure in the PR.

## How to upgrade Harbor

1. Change the pin in `pyproject.toml` and run `uv lock`.
2. Run `uv run pytest tests/e2e -m contract`. Each failure names one behaviour that changed.
3. For each failure, change `harbor_adapter` (or `harness/agents.py`), never the test's expectation, unless the owner rules that the new behaviour is acceptable.
4. Run the full end-to-end tier, then one live test.

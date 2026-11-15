# Agent instructions

`harbor-research-runner` (`hrr`) runs versions of a coding-agent harness on benchmark tasks under one model, and records every model request through a local proxy. Read these before changing anything:

- `docs/architecture.md`: modules, the import rule, commands, every file format, the validity rules. It is the contract. If a brief and this file disagree, stop and report.
- `docs/decisions.md`: the rulings behind the design.
- `docs/testing.md`: the four test tiers and the rules for tests.

## Commands

| Task | Command |
|---|---|
| Install | `uv sync --all-extras --locked` |
| Gate (format, lint, types, unit and integration tests, lock) | `uv run gate` |
| End-to-end tier (needs Docker) | `uv run gate --only e2e` |
| Contract tests only | `uv run pytest tests/e2e -m contract` |
| Repository checks | `uv run check` |
| One test file | `uv run pytest tests/unit/records/test_jsonl.py` |
| Format | `uv run ruff format` |

Use `uv` for everything. No pip, no direct `python`. Never pipe a gate or test command through `tail` or `head`: the pipe hides the exit code. Quote the real exit code.

## Rules for every change

1. One step, one commit. Work only in your worktree. Do not push and do not use `gh`.
2. Make no design decisions. A missing detail is a reason to stop and report, not to choose.
3. Do not add a dependency, a command-line flag, an environment variable, a file format field or a module that your brief does not name. Do not touch a module your brief does not name.
4. Tests first. Write the test, run it, see it fail for the reason you expect, then write the code. Put the failing output in your report.
5. No comments in code unless the brief asks. Docstrings on public functions are fine. Log with `logging`; never `print` in the package.
6. Add one changelog fragment: `changes/<step number>.<feature|fix|doc|misc>.md`, one or two sentences. Never edit `CHANGELOG.md`.
7. Never commit anything under `runs/`. Never print or commit an API key.
8. Finish by running your brief's acceptance command and `uv run gate`, unpiped. Report both exit codes.

## Rules for tests (full text in `docs/testing.md`)

- Assert on outputs: return values, files on disk, exit codes. Not on which internal functions were called.
- Fakes exist at three boundaries only: the upstream (`tests/support/fake_upstream.py`), the harness (`harness/fake.py`) and the clock (pass a `now` function or a timestamp). Do not patch, mock or monkeypatch any `harbor_research_runner` module. One narrow exception applies to the unchanged process-group lifecycle port: stub `os.killpg` directly and a fake `time.sleep` clock, call the public function with a plain process-like value, and start no process or signal no real PID. A callable external-command input is also allowed when the public API named by a brief accepts one, as the manifest API does.
- Unit tests do not sleep, start processes or open sockets; `tests/conftest.py` enforces this and a 0.1 s CPU budget. Integration tests have 5 s, end-to-end tests 5 minutes.
- Unit tests mirror the source path: `src/harbor_research_runner/records/jsonl.py` is tested by `tests/unit/records/test_jsonl.py`.
- One behaviour per test, named for the behaviour. No issue numbers in names or comments.

## Branches

`dev` is the integration branch; every change is a pull request into `dev`, squash-merged. `main` moves only at a release.

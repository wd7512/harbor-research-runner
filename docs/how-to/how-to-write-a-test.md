# How to write a test

Choose the lowest test tier that can produce the behaviour, and assert on observable results such as return values, files, or exit codes.

1. **Choose the tier.**

   | Tier | Put it in | Use it for | Budget |
   |---|---|---|---|
   | Unit | `tests/unit/` | One function or class called directly with real inputs. | 0.1 seconds of CPU per test. |
   | Integration | `tests/integration/` | The real proxy with the scripted fake upstream, or two modules working together through files. | 5 seconds wall time. |
   | End-to-end | `tests/e2e/` | Harbor, Docker, the bundled task, fake harness, and fake upstream together. Contract tests are marked `contract`. | 5 minutes wall time. |
   | Live | `tests/live/` | Real OpenCode, a real model, and one real task. | Run by the owner before release. |

2. **Use the permitted boundaries and real inputs.** Use the upstream, harness, and clock fakes at their named boundaries. Do not patch or monkeypatch product modules. Unit tests must not sleep, start processes, or open sockets. Use real record shapes from `docs/architecture.md`; parser tests should use a representative fixture. Unit tests mirror the source tree, such as `src/harbor_research_runner/records/jsonl.py` and `tests/unit/records/test_jsonl.py`.

3. **Test one behaviour and name it clearly.** For example: `test_trial_with_one_tolerated_error_is_valid`. Keep each test within its tier budget, and keep added fixtures below 200 KB with their origin documented in `tests/fixtures/README.md`.

4. **Run the targeted test and relevant tier.** Run a single test file with:

```sh
uv run pytest tests/unit/records/test_jsonl.py
```

   Run the standard gate with `uv run gate`. Run the end-to-end tier with `uv run gate --only e2e`. Do not pipe these commands through `head` or `tail`; record the command's actual exit code.

# How to add a harness

1. **Subclass `PinnedHarness` and implement its four methods.** `PinnedHarness` is the shared Harbor agent base.

| Method | Responsibility |
|---|---|
| `config_files(facts, model, proxy_base_url) -> dict[str, str]` | Return the configuration files to write for this harness. |
| `run_command(facts, model, instruction) -> str` | Return the command that starts the harness for the task. |
| `async probe(environment) -> HarnessFacts` | Inspect the installed harness in the task environment and return its version and capabilities. |
| `process_pattern() -> str` | Identify the harness process used by the in-container stop script. |

   The base handles installation and execution: ordinary harnesses verify their image against the commit manifest, extract and upload the binary, and probe it; execution writes config, runs the command, and stops the process tree on cancellation.

2. **Add a manifest entry for the pinned commit and image.**

   `hrr build-harness` adds the image entry to `harness-manifest.toml`. Each commit records informational version and withdrawn state, plus image metadata for each supported architecture. A manifest entry has this shape:

```toml
[opencode."0123456789abcdef0123456789abcdef01234567"]
version = "1.2.3"
withdrawn = false

[opencode."0123456789abcdef0123456789abcdef01234567".images.amd64]
ref = "hrr/opencode:0123456789ab-amd64"
digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
build_method = "source"
built_at = "2026-11-12"
```

   The commit is 40 lowercase hexadecimal characters. `build_method` is `source` or `release-package`; the supported image architectures are `amd64` and `arm64`. A withdrawn commit may include `withdrawn_reason`.

3. **Use the fake harness as a worked example.**

   `FakeHarnessAgent` is the test fixture. It subclasses `PinnedHarness`, provides the four methods above, and requires no image or manifest. Its `harness_files` supplies `fake_script.py`; its config is empty, probe returns fixed facts, its process pattern is `fake_script.py`, and its command runs that script. Its optional `fake` settings control request count, exit code, sleep, whether to ignore stop, continued requests, and whether to solve the task. Ordinary harnesses reject the `fake` setting.

   Use the fake harness to exercise the adapter seam without a model image. Keep it as test support; the shipped real harness is OpenCode.

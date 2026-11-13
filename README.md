# harbor-research-runner

`hrr` compares versions of a coding-agent harness. It runs each version on the same benchmark tasks with the same model, through [Harbor](https://pypi.org/project/harbor/), and sends every model request through a local proxy that records it. The result is a pass rate per version, with the token use behind it, and a record of which trials were disturbed by something other than the harness.

**Status: under construction.** Nothing here runs a study yet. The design is fixed and the modules are being built step by step.

## What it will do

| Command | Does |
|---|---|
| `hrr build-harness` | Builds a harness image from one upstream commit |
| `hrr check-machine` | Checks which tasks pass their reference solution on this machine |
| `hrr run` | Launches or resumes a run |
| `hrr build-evidence` | Turns a finished run folder into an evidence bundle |
| `hrr explore` | Serves a read-only comparison of evidence bundles |
| `hrr clean-docker` | Lists or removes leftover trial containers |

## Read more

- [Architecture](docs/architecture.md): modules, file formats, the rules for judging a trial
- [Decisions](docs/decisions.md): the rulings behind the design
- [Testing](docs/testing.md): test tiers and contributor rules
- [How-to guides](docs/how-to/): [check a new host](docs/how-to/how-to-check-a-new-host.md), running studies, reading evidence, maintaining tasks, tests, harnesses, and models

## Develop

```sh
uv sync --all-extras --locked
uv run gate
```

Licensed under Apache-2.0.

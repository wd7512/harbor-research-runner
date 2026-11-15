# harbor-research-runner

`hrr` compares versions of a coding-agent harness on benchmark tasks against a nominal model service. Every model request passes through a local proxy that records it. Independent instruments judge trial validity, and `hrr build-evidence` produces evidence bundles for comparison.

**Status: 0.1.0.** It has run a pilot study on a single Docker host: 10/10 trials completed, and all 12 verdicts were valid. The paid study has not run yet.

## Commands

| Command | Does |
|---|---|
| `hrr build-harness` | Builds a harness image from one upstream commit |
| `hrr check-machine` | Checks which tasks pass their reference solution on this machine |
| `hrr run` | Launches or resumes a study run |
| `hrr build-evidence` | Builds an evidence bundle from a run folder |
| `hrr explore` | Serves a read-only comparison of evidence bundles |
| `hrr clean-docker` | Lists or removes leftover trial containers |

## Read more

- [Architecture](docs/architecture.md): modules, file formats, and trial validity rules
- [Check a new host](docs/how-to/how-to-check-a-new-host.md)
- [Run a study](docs/how-to/how-to-run-a-study.md)
- [Decisions](docs/decisions.md)
- [Testing](docs/testing.md)

## Develop

```sh
uv sync --all-extras --locked
uv run gate
```

Licensed under Apache-2.0.

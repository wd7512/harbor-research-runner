# harbor-research-runner

`hrr` compares versions of a coding-agent harness. It runs each version on the same benchmark tasks, through [Harbor](https://pypi.org/project/harbor/), against a nominal model service, and sends every model request through a local proxy that records it. Independent instruments judge whether each trial was disturbed by something other than the harness. The result is an evidence bundle: a pass rate per version, the token use behind it, and the record of which trials were valid.

**Status: 0.1.0.** It has run a pilot study (5 tasks, two harness versions) on a single Docker host, with every trial valid. The paid study has not run yet.

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
- [Run on a Slurm cluster](docs/how-to/how-to-run-on-a-slurm-cluster.md)
- [Run a study](docs/how-to/how-to-run-a-study.md)
- [Decisions](docs/decisions.md)
- [Testing](docs/testing.md)

## Develop

```sh
uv sync --all-extras --locked
uv run gate
```

Licensed under Apache-2.0.

# How to make a release

This repository is prepared for `0.1.0`; the owner makes the release after the first paid study passes. This guide describes the release steps without performing them.

## Align the version

The package version is declared by `project.version` in `pyproject.toml`. There is no package-level `__version__` in the current source; runtime version reporting uses installed package metadata. `uv.lock` also records the project version in the `harbor-research-runner` package entry. Keep these values equal. If a package-level `__version__` is introduced, keep it equal as well.

For a version bump, edit `pyproject.toml`, then regenerate and verify the lock:

```sh
uv lock
uv lock --check
uv run pytest tests/unit/test_skeleton.py
```

The unit test checks that the project version and its lockfile entry agree.

## Preview and write the changelog

Towncrier reads fragments from `changes/`, writes `CHANGELOG.md`, and uses the configured `Added`, `Fixed`, `Documentation`, and `Internal` sections. Preview the release before consuming the fragments:

```sh
uv run towncrier build --draft --version 0.1.0
```

Collate on a branch from `dev` and merge it into `dev` by pull request before the release merge. Towncrier deletes the fragments it consumes, so collating on the release branch would leave them on `dev` and the next release would list them again:

```sh
git fetch origin
git switch -c release-notes/0.1.0 origin/dev
uv run towncrier build --version 0.1.0
git commit -am "release: changelog for 0.1.0"
git push origin release-notes/0.1.0
```

Use the release version in both towncrier commands. That pull request only deletes fragments and edits `CHANGELOG.md`, which `[tool.devtools.check] changelog_exempt` allows without a new fragment.

## Merge and verify CI

After the paid study passes, create a release branch from `main` and merge `dev` into it with a normal merge commit:

```sh
git fetch origin
git switch -c release/0.1.0 origin/main
git merge --no-ff origin/dev
git push --set-upstream origin release/0.1.0
```

Open a pull request from `release/0.1.0` to `main` and select the merge-commit option. CI runs for that pull request. It must show successful `gate` jobs on Ubuntu and macOS (each running the repository-check command) and, once the Docker end-to-end job is in the workflow, a successful `e2e` job. The merge to `main` runs those same CI jobs again. A pull request targeting `dev` additionally runs `uv run check --checks changelog-fragment`; CI does not run that extra check for `main`.

Run the same checks locally before opening the pull request:

```sh
uv sync --all-extras --locked
uv run gate
uv run check --checks test-names,test-layout,filename-portability,secrets,untracked-paths
```

## Tag the release

After the merge is on `main` and its CI checks pass, create and push the annotated release tag:

```sh
git switch main
git pull --ff-only origin main
git tag -a 0.1.0 -m "0.1.0"
git push origin 0.1.0
```

Replace `0.1.0` consistently if preparing a later release.

A run now keeps a write-once copy of its machine check as `machine-check.json` in the run folder, from the same bytes whose SHA-256 is in `run-metadata.json`, so `hrr build-evidence` finds it.

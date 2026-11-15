Add `hrr verify-evidence` to check evidence bundle invariants independently. Evidence builds now run the checker after writing and return exit code 5 when the written bundle is inconsistent.

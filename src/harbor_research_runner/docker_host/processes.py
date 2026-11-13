from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path


def pids_naming_run(run_dir: Path, run: Callable = subprocess.run) -> list[int]:
    result = run(
        ["ps", "-ww", "-axo", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
        check=True,
    )
    run_path = str(run_dir)
    parents: dict[int, int] = {}
    naming: set[int] = set()
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) < 2:
            continue
        pid, ppid = int(fields[0]), int(fields[1])
        parents[pid] = ppid
        if len(fields) == 3 and run_path in fields[2]:
            naming.add(pid)
    return sorted(naming - _ancestry(os.getpid(), parents))


def _ancestry(pid: int, parents: dict[int, int]) -> set[int]:
    chain = {pid}
    while (pid := parents.get(pid, 0)) > 1 and pid not in chain:
        chain.add(pid)
    return chain

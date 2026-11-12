#!/bin/sh
set -eu

mkdir -p /logs/verifier
python3 - <<'PY'
from pathlib import Path

try:
    content = Path("/app/hello.txt").read_bytes()
except OSError:
    content = None
reward = 1 if content in (b"hello", b"hello\n") else 0
Path("/logs/verifier/reward.txt").write_text(f"{reward}\n", encoding="ascii")
PY

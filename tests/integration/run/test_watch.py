from __future__ import annotations

import json
import signal
from pathlib import Path
from types import SimpleNamespace

from harbor_research_runner.records.limits import Limits
from harbor_research_runner.run.watch import watch


class _Process:
    pid = 987654321

    def __init__(self, returncode: int | None = None):
        self.returncode = returncode
        self.stops = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def _session(run_dir: Path):
    limits = Limits(max_set_asides_per_slot=2, verifier_timeout_multiplier=1.0)
    events = run_dir / "run-events.jsonl"

    def event(value):
        with events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value.to_row(), sort_keys=True) + "\n")

    return SimpleNamespace(
        run_dir=run_dir,
        limits=limits,
        event=event,
        job=SimpleNamespace(machine_check=run_dir / "machine-check.json"),
        metadata=SimpleNamespace(task_set={"tasks": [{"name": "task-a"}]}),
        proxy_alive=lambda: True,
    )


def _write_sample(run_dir: Path) -> None:
    (run_dir / "resource-stats.jsonl").write_text(
        json.dumps(
            {
                "type": "sample",
                "t": 100.0,
                "mem_available_mb": 8192,
                "swap_used_mb": 0,
                "disk_free_gb": 100.0,
                "load1": 0.0,
                "trial_containers": 0,
                "docker_ok": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "machine-check.json").write_text(
        json.dumps(
            {
                "created_t": 1.0,
                "tool_version": "0.1.0",
                "harbor_version": "0.23.0",
                "host_fingerprint": "host",
                "task_source": {},
                "concurrency": 1,
                "tasks": [{"name": "task-a", "agent_budget_s": 10.0, "verifier_budget_s": 10.0}],
                "included": ["task-a"],
                "verifier_timeout_multiplier": 1.0,
            }
        ),
        encoding="utf-8",
    )


def test_quota_on_second_poll_halts_and_stops_process_once(tmp_path: Path, monkeypatch) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()
    ledger = run_dir / "proxy-ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    _write_sample(run_dir)
    session = _session(run_dir)
    process = _Process()
    signals: list[tuple[int, int]] = []

    def signal_group(pid, sig):
        signals.append((pid, sig))
        if sig == signal.SIGINT:
            process.returncode = 0
        elif sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr("os.killpg", signal_group)
    polls = 0

    def sleep(_seconds):
        nonlocal polls
        polls += 1
        if polls == 2:
            with ledger.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "type": "request_end",
                            "seq": 1,
                            "t": 101.0,
                            "trial": "trial-a",
                            "request_id": "request-a",
                            "http_status": 429,
                            "upstream_status": "quota_exhausted",
                            "upstream_http_status": 429,
                            "served_model": "model",
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "cached_read_tokens": 0,
                            "completion_chars": 1,
                            "first_chunk_s": 0.1,
                            "first_content_s": 0.1,
                            "duration_s": 0.1,
                            "failovers": 0,
                            "error_excerpt": None,
                        }
                    )
                    + "\n"
                )

    result = watch(session, process, job_dir, 100.0, poll_s=1.0, now=lambda: 102.0, sleep=sleep)

    assert result.kind == "halted"
    assert result.halt_reason == "quota_exhausted"
    assert signals.count((process.pid, signal.SIGINT)) == 1
    assert polls == 2


def test_exited_process_returns_its_status(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()
    (run_dir / "proxy-ledger.jsonl").write_text("", encoding="utf-8")
    _write_sample(run_dir)
    process = _Process(returncode=0)

    result = watch(
        _session(run_dir),
        process,
        job_dir,
        100.0,
        poll_s=1.0,
        now=lambda: 100.0,
        sleep=lambda _seconds: None,
    )

    assert result.kind == "exited"
    assert result.returncode == 0

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from harbor_research_runner.records.run_metadata import RunMetadata, write_once


def _metadata(index: int) -> RunMetadata:
    return RunMetadata(
        run_id=f"run-{index}",
        created_t=1.0,
        tool_version="v",
        tool_commit="c",
        harbor_version="h",
        job={},
        job_file_sha256="a" * 64,
        model_card={},
        arms=[],
        task_set={},
        machine_check_sha256="b" * 64,
        host={},
        proxy={"host": "localhost", "port": 1},
        limits={},
    )


def _attempt(index: int, path: str, barrier: threading.Barrier) -> tuple[int, str]:
    barrier.wait()
    try:
        write_once(Path(path), _metadata(index))
    except FileExistsError:
        return index, "exists"
    else:
        return index, "written"


def test_concurrent_write_once_publishes_exactly_one_writer(tmp_path):
    path = tmp_path / "run-metadata.json"
    writers = 16
    barrier = threading.Barrier(writers, timeout=5)

    with ThreadPoolExecutor(max_workers=writers) as pool:
        futures = [pool.submit(_attempt, index, str(path), barrier) for index in range(writers)]
        outcomes = [future.result(timeout=5) for future in futures]

    assert [status for _, status in outcomes].count("written") == 1
    assert [status for _, status in outcomes].count("exists") == writers - 1
    winner = next(index for index, status in outcomes if status == "written")
    assert path.read_bytes() == _metadata(winner).to_json().encode("utf-8")
    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]

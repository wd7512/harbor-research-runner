import pytest

from harbor_research_runner.records.run_metadata import RunMetadata, read, write_once


def _metadata():
    return RunMetadata(
        run_id="run",
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


def test_run_metadata_round_trips():
    metadata = _metadata()

    assert RunMetadata.from_row(metadata.to_row()) == metadata


@pytest.mark.parametrize("field", list(_metadata().to_row()))
def test_run_metadata_parser_rejects_missing_field(field):
    row = _metadata().to_row()
    row.pop(field)

    with pytest.raises(ValueError):
        RunMetadata.from_row(row)


def test_run_metadata_parser_rejects_unknown_field():
    row = _metadata().to_row()
    row["extra"] = True

    with pytest.raises(ValueError):
        RunMetadata.from_row(row)


def test_run_metadata_write_is_atomic(tmp_path):
    metadata = _metadata()
    path = tmp_path / "run-metadata.json"
    path.write_text('{"existing":true}')

    with pytest.raises(FileExistsError):
        write_once(path, metadata)
    assert path.read_text() == '{"existing":true}'

    path.unlink()
    write_once(path, metadata)
    assert read(path) == metadata
    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]


def test_write_once_refuses_existing_dangling_symlink(tmp_path):
    metadata = _metadata()
    missing = tmp_path / "missing.json"
    path = tmp_path / "run-metadata.json"
    path.symlink_to(missing)

    with pytest.raises(FileExistsError):
        write_once(path, metadata)

    assert path.is_symlink()
    assert path.readlink() == missing
    assert not missing.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]

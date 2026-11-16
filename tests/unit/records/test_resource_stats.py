import pytest

from harbor_research_runner.records.resource_stats import Sample


def _sample():
    return Sample(
        t=1.0,
        mem_available_mb=2,
        swapped_out_pages=3,
        disk_free_gb=4.0,
        load1=0.5,
        trial_containers=1,
        docker_ok=True,
    )


def test_resource_sample_round_trips():
    sample = _sample()

    assert Sample.from_row(sample.to_row()) == sample


@pytest.mark.parametrize(
    "field", [field for field in _sample().to_row() if field != "swapped_out_pages"]
)
def test_resource_sample_parser_rejects_missing_field(field):
    row = _sample().to_row()
    row.pop(field)

    with pytest.raises(ValueError):
        Sample.from_row(row)


def test_resource_sample_parser_rejects_unknown_field():
    row = _sample().to_row()
    row["extra"] = True

    with pytest.raises(ValueError):
        Sample.from_row(row)


def test_resource_sample_without_swapouts_defaults_to_zero():
    row = _sample().to_row()
    row.pop("swapped_out_pages")
    row["swap_used_mb"] = 3802

    assert Sample.from_row(row).swapped_out_pages == 0

import pytest

from harbor_research_runner.records.limits import Limits, host_breach


def _limits(**overrides):
    return Limits.from_dict(overrides, repetitions=1, verifier_timeout_multiplier=1.5)


def test_limits_defaults_include_repetition_derived_cap():
    limits = Limits.from_dict({}, repetitions=3, verifier_timeout_multiplier=1.5)

    assert limits.max_set_asides_per_slot == 6
    assert limits.tolerated_errors == 1
    assert limits.trial_output_rate_min == 2.0
    assert limits.verifier_timeout_multiplier == 1.5
    assert limits.memory_reserve_mb == 4096
    assert limits.host_reserve_mb == 12288
    assert limits.memory_warn_multiplier == 1.0
    assert limits.memory_kill_multiplier == 1.0


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"memory_warn_multiplier": 0}, "positive"),
        ({"memory_kill_multiplier": float("inf")}, "finite"),
        ({"memory_warn_multiplier": True}, "positive"),
        ({"memory_warn_multiplier": 2, "memory_kill_multiplier": 1}, "less than or equal"),
    ],
)
def test_memory_multipliers_must_be_finite_positive_and_ordered(overrides, message):
    with pytest.raises(ValueError, match=message):
        _limits(**overrides)


def test_limits_rejects_job_override_of_machine_value():
    with pytest.raises(ValueError):
        Limits.from_dict(
            {"verifier_timeout_multiplier": 2.0},
            repetitions=1,
            verifier_timeout_multiplier=1.5,
        )


def test_limits_rejects_unknown_fields():
    with pytest.raises(ValueError):
        Limits.from_dict({"unknown": 1}, repetitions=1, verifier_timeout_multiplier=1.5)


def test_limits_round_trips():
    limits = _limits()

    assert Limits.from_row(limits.to_row()) == limits


def test_limits_row_recorded_before_86_loads_with_defaults():
    row = _limits().to_row()
    row.pop("max_swapped_out_pages")
    row.pop("memory_reserve_mb")
    row.pop("host_reserve_mb")
    row["max_swap_used_mb"] = 512
    row["min_spare_memory_mb"] = 2048

    limits = Limits.from_row(row)

    assert limits.max_swapped_out_pages == 0
    assert limits.memory_reserve_mb == 4096
    assert limits.host_reserve_mb == 12288


def test_limits_row_with_retired_zero_billed_window_loads_with_new_default():
    row = _limits().to_row()
    row.pop("halt_after_consecutive_failed_requests", None)
    row["zero_billed_window_s"] = 900

    limits = Limits.from_row(row)

    assert limits.halt_after_consecutive_failed_requests == 10


@pytest.mark.parametrize(
    "field",
    [
        name
        for name in _limits().to_row()
        if name
        not in {
            "max_swapped_out_pages",
            "memory_reserve_mb",
            "halt_after_consecutive_failed_requests",
            "host_reserve_mb",
            "memory_warn_multiplier",
            "memory_kill_multiplier",
        }
    ],
)
def test_limits_parser_rejects_missing_field(field):
    row = _limits().to_row()
    row.pop(field)

    with pytest.raises(ValueError):
        Limits.from_row(row)


def test_limits_parser_rejects_unknown_field():
    row = _limits().to_row()
    row["extra"] = 1

    with pytest.raises(ValueError):
        Limits.from_row(row)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("swapped_out_pages", 1),
        ("disk_free_gb", 1),
        ("docker_ok", False),
    ],
)
def test_host_breach_detects_each_limit(field, bad_value):
    limits = _limits(max_swapped_out_pages=0, min_free_disk_gb=10)
    sample = {
        "mem_available_mb": 100,
        "swapped_out_pages": 0,
        "disk_free_gb": 10,
        "docker_ok": True,
    }
    sample[field] = bad_value

    assert host_breach(sample, limits)


def test_host_breach_accepts_values_at_limits():
    limits = _limits(max_swapped_out_pages=0, min_free_disk_gb=10)

    assert not host_breach(
        {"mem_available_mb": 100, "swapped_out_pages": 0, "disk_free_gb": 10, "docker_ok": True},
        limits,
    )


def test_low_available_memory_without_swapouts_does_not_breach():
    limits = _limits()

    assert not host_breach(
        {"mem_available_mb": 1, "swapped_out_pages": 0, "disk_free_gb": 100, "docker_ok": True},
        limits,
    )


def test_swapouts_still_breach_with_available_memory():
    limits = _limits()

    assert host_breach(
        {"mem_available_mb": 16384, "swapped_out_pages": 1, "disk_free_gb": 100, "docker_ok": True},
        limits,
    )


def test_stale_occupied_swap_does_not_breach_without_swapouts():
    limits = _limits()

    assert not host_breach(
        {"mem_available_mb": 8192, "swap_used_mb": 3802, "disk_free_gb": 100, "docker_ok": True},
        limits,
    )

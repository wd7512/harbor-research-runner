from __future__ import annotations

import pytest

from harbor_research_runner.model_proxy.timing import StreamClock, output_rate


def test_output_rate_is_null_for_tiny_window() -> None:
    assert output_rate(20, 10.0, 10.4) is None


def test_output_rate_uses_the_content_window() -> None:
    assert output_rate(10, 2.0, 4.0) == 5.0


def test_output_rate_is_measured_at_the_minimum_window() -> None:
    assert output_rate(2, 1.0, 1.5) == 4.0


def test_output_rate_is_null_without_first_content() -> None:
    assert output_rate(10, None, 4.0) is None


def test_stream_clock_records_first_chunk_first_content_and_duration() -> None:
    times = iter([10.0, 10.25, 10.75, 12.0])
    clock = StreamClock(now=lambda: next(times))

    clock.mark_chunk()
    clock.mark_content()
    clock.mark_content()
    duration = clock.finish()

    assert clock.first_chunk_s == pytest.approx(0.25)
    assert clock.first_content_s == pytest.approx(0.75)
    assert clock.duration_s == pytest.approx(2.0)
    assert duration == pytest.approx(2.0)

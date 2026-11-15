from __future__ import annotations

import threading

import pytest

from harbor_research_runner.model_proxy.pacing import (
    Pacer,
    estimate_prompt_tokens,
    estimate_request_tokens,
    expected_completion_draw,
    payload_completion_cap,
)


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.value = now
        self.lock = threading.Lock()

    def now(self) -> float:
        with self.lock:
            return self.value

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.value += seconds


@pytest.mark.parametrize(
    ("limit", "requests", "expected_waits", "expected_now"),
    [
        (100, [40, 60], [0.0, 0.0], 1000.0),
        (100, [80, 80], [0.0, 60.0], 1060.0),
        (100, [10_000, 1], [0.0, 60.0], 1060.0),
        (100, [0], [0.0], 1000.0),
        (None, [100, 80], [0.0, 0.0], 1000.0),
    ],
)
def test_rolling_window_cases(limit, requests, expected_waits, expected_now) -> None:
    clock = FakeClock()
    pacer = Pacer(limit, clock.now, clock.sleep)

    assert [pacer.wait_for(tokens) for tokens in requests] == expected_waits
    assert clock.now() == expected_now


@pytest.mark.parametrize(
    ("body", "expected", "exact"),
    [
        (
            {
                "messages": [{"role": "user", "content": "a" * 400}],
                "tools": [{"type": "function", "function": {"name": "x", "parameters": {}}}],
            },
            100,
            False,
        ),
        (
            {"input": [{"role": "user", "content": [{"type": "input_text", "text": "b" * 80}]}]},
            20,
            True,
        ),
    ],
)
def test_prompt_token_estimate_cases(body, expected, exact) -> None:
    estimate = estimate_prompt_tokens(body)
    assert estimate == expected if exact else estimate >= expected


def test_second_request_waits_until_the_first_leaves_the_window() -> None:
    clock = FakeClock()
    pacer = Pacer(1000, clock.now, clock.sleep)

    assert pacer.wait_for(600) == 0.0
    assert pacer.wait_for(600) == 60.0
    assert clock.now() == 1060.0


def test_restart_cooldown_delays_only_the_first_request() -> None:
    clock = FakeClock()
    pacer = Pacer(1000, clock.now, clock.sleep, cooldown_until=1030.0)

    assert pacer.wait_for(100) == 30.0
    assert pacer.wait_for(100) == 0.0


def test_record_replaces_the_estimate_with_actual_usage() -> None:
    clock = FakeClock()
    pacer = Pacer(1000, clock.now, clock.sleep)

    pacer.wait_for(900)
    pacer.record(100)

    assert pacer.wait_for(900) == 0.0


def test_concurrent_requests_share_the_rolling_window() -> None:
    clock = FakeClock()
    pacer = Pacer(1000, clock.now, clock.sleep)
    barrier = threading.Barrier(3)
    waits: list[float] = []

    def request() -> None:
        barrier.wait()
        waits.append(pacer.wait_for(600))

    threads = [threading.Thread(target=request) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(waits) == [0.0, 60.0]
    assert clock.now() == 1060.0


def test_record_does_not_wait_for_another_request_to_finish_pacing() -> None:
    sleep_started = threading.Event()
    release_sleep = threading.Event()
    clock = [1000.0]

    def blocking_sleep(seconds: float) -> None:
        sleep_started.set()
        release_sleep.wait()
        clock[0] += seconds

    pacer = Pacer(1000, lambda: clock[0], blocking_sleep)
    pacer.wait_for(1000)
    waiting_request = threading.Thread(target=lambda: pacer.wait_for(1000))
    waiting_request.start()
    assert sleep_started.wait(1)

    recording = threading.Thread(target=lambda: pacer.record(100))
    recording.start()
    recording.join(0.05)
    blocked = recording.is_alive()
    release_sleep.set()
    waiting_request.join(1)
    recording.join(1)

    assert not blocked
    assert not waiting_request.is_alive()
    assert not recording.is_alive()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"max_tokens": 123}, 123),
        ({"max_completion_tokens": 5}, 5),
        ({"max_output_tokens": 7}, 7),
        ({"max_tokens": 0}, None),
        ({}, None),
    ],
)
def test_payload_completion_cap_reads_every_supported_key(body, expected) -> None:
    assert payload_completion_cap(body) == expected


def test_expected_completion_draw_prefers_payload_cap_under_model_ceiling() -> None:
    assert expected_completion_draw(65536, {"max_tokens": 5000}) == 5000
    assert expected_completion_draw(65536, {"max_tokens": 100_000}) == 65536
    assert expected_completion_draw(65536, {}) == 65536


def test_expected_completion_draw_without_model_ceiling_uses_payload_cap() -> None:
    assert expected_completion_draw(None, {"max_tokens": 5000}) == 5000
    assert expected_completion_draw(None, {}) == 0


def test_estimate_request_tokens_sums_prompt_and_completion_draw() -> None:
    body = {"messages": [{"role": "user", "content": "a" * 400}], "max_tokens": 5000}

    assert estimate_request_tokens(body, 65536) == estimate_prompt_tokens(body) + 5000

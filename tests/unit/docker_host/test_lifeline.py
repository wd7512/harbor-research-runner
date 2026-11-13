import os
import threading

from harbor_research_runner.docker_host.lifeline import start_lifeline_watch, watch_lifeline


def test_closed_lifeline_sets_stopped_and_closes_the_read_end():
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    stopped = threading.Event()

    watch_lifeline(read_fd, stopped)

    assert stopped.is_set()
    try:
        os.fstat(read_fd)
    except OSError:
        pass
    else:
        raise AssertionError("lifeline read end was left open")


def test_open_lifeline_keeps_running_until_the_writer_closes():
    read_fd, write_fd = os.pipe()
    stopped = threading.Event()

    start_lifeline_watch(read_fd, stopped)

    assert not stopped.wait(0.02)
    os.close(write_fd)
    assert stopped.wait(1.0)


def test_no_lifeline_starts_no_watch():
    stopped = threading.Event()

    start_lifeline_watch(None, stopped)

    assert not stopped.is_set()

import subprocess

from harbor_research_runner.docker_host import images


class RecordingRun:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")


def test_container_operations_send_exact_argv_with_their_timeout() -> None:
    run = RecordingRun()

    images.create("hrr/opencode:abc-amd64", "hrr-tmp", run, 600)
    images.copy("hrr-tmp:/usr/local/.", "/out/.", run, 600)
    images.remove("hrr-tmp", run, 600)

    assert run.calls == [
        (
            ["docker", "create", "--name", "hrr-tmp", "hrr/opencode:abc-amd64"],
            {"capture_output": True, "text": True, "check": False, "timeout": 600},
        ),
        (
            ["docker", "cp", "hrr-tmp:/usr/local/.", "/out/."],
            {"capture_output": True, "text": True, "check": False, "timeout": 600},
        ),
        (
            ["docker", "rm", "hrr-tmp"],
            {"capture_output": True, "text": True, "check": False, "timeout": 600},
        ),
    ]


def test_inspections_send_no_timeout() -> None:
    run = RecordingRun()

    images.image_digest("ref", run)
    images.host_architecture(run)

    assert run.calls == [
        (
            ["docker", "image", "inspect", "--format", "{{.Id}}", "ref"],
            {"capture_output": True, "text": True, "check": False},
        ),
        (
            ["docker", "info", "--format", "{{.Architecture}}"],
            {"capture_output": True, "text": True, "check": False},
        ),
    ]

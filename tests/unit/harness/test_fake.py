from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

import pytest
from harbor.agents.factory import AgentFactory
from harbor.models.trial.config import AgentConfig

from harbor_research_runner.harness.agents import agent_settings
from harbor_research_runner.harness.fake import FakeBehavior, FakeHarnessAgent
from harbor_research_runner.harness.opencode_eras import HarnessFacts

_COMMIT = "a" * 40
_MANIFEST = "unused/harness-manifest.toml"
_MODEL = "ahe/mimo-v2.5"
_IMPORT_PATH = "harbor_research_runner.harness.fake:FakeHarnessAgent"
_PROXY_URL = "http://host.docker.internal:8766/trial/say-hello__Ab3dE9x/v1"

_CARD = {
    "id": "opencodego/mimo-v2.5-pro",
    "upstream_model": "mimo-v2.5",
    "variants": {},
    "default_variant": None,
}


class _Result:
    def __init__(self, return_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeEnvironment:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.uploads: list[tuple[Path, str]] = []

    async def exec(self, command: str, **kwargs):
        self.calls.append({"command": command, **kwargs})
        if command == "uname -m":
            return _Result(0, "x86_64\n")
        return _Result()

    async def upload_dir(self, source_dir: Path, target_dir: str) -> None:
        self.uploads.append((source_dir, target_dir))


def _settings(*, fake: dict | None = None) -> dict:
    return agent_settings(
        arm_label="fake",
        harness="fake",
        commit=_COMMIT,
        manifest_path=_MANIFEST,
        container_host="host.docker.internal",
        proxy_port=8766,
        variant=None,
        model_card=dict(_CARD),
        fake=fake,
    )


def _agent_config(settings: dict) -> AgentConfig:
    return AgentConfig(import_path=_IMPORT_PATH, model_name=_MODEL, kwargs=settings)


@pytest.fixture
def make_agent(tmp_path: Path):
    def make(*, fake: dict | None = None) -> FakeHarnessAgent:
        agent = FakeHarnessAgent(logs_dir=tmp_path, model_name=_MODEL, **_settings(fake=fake))
        agent.session_id = "say-hello__Ab3dE9x__agent"
        return agent

    return make


def _facts() -> HarnessFacts:
    return HarnessFacts(version="fake", help_text="", era="fake")


def _arguments(agent: FakeHarnessAgent) -> list[str]:
    return shlex.split(agent.run_command(_facts(), _MODEL, "ignored instruction"))


def _value(arguments: list[str], flag: str) -> str:
    return arguments[arguments.index(flag) + 1]


def test_default_behavior_is_pinned() -> None:
    behavior = FakeBehavior()

    assert behavior.requests == 1
    assert behavior.exit_code == 0
    assert behavior.sleep_s == 0
    assert behavior.ignore_stop is False
    assert behavior.keep_requesting_s == 0
    assert behavior.solve is True


def test_construction_without_a_manifest_file_succeeds(tmp_path: Path) -> None:
    agent = FakeHarnessAgent(logs_dir=tmp_path, model_name=_MODEL, **_settings())

    assert agent.commit == _COMMIT


def test_harness_files_contains_only_the_packaged_script(make_agent) -> None:
    folder = make_agent().harness_files()

    packaged_script = (
        Path(__file__).parents[3] / "src/harbor_research_runner/harness/fake_script.py"
    )
    assert sorted(path.name for path in folder.iterdir()) == ["fake_script.py"]
    assert (folder / "fake_script.py").read_bytes() == packaged_script.read_bytes()


def test_install_uploads_the_script_folder_without_a_manifest(make_agent) -> None:
    agent = make_agent()
    environment = _FakeEnvironment()

    asyncio.run(agent.install(environment))

    assert len(environment.uploads) == 1
    uploaded_folder, target = environment.uploads[0]
    assert target == "/usr/local"
    assert sorted(path.name for path in uploaded_folder.iterdir()) == ["fake_script.py"]


def test_probe_returns_fixed_facts(make_agent) -> None:
    facts = asyncio.run(make_agent().probe(_FakeEnvironment()))

    assert facts == HarnessFacts(version="fake", help_text="", era="fake")


def test_config_files_are_empty(make_agent) -> None:
    assert make_agent().config_files(_facts(), _MODEL, _PROXY_URL) == {}


def test_process_pattern_is_the_script(make_agent) -> None:
    assert make_agent().process_pattern() == "fake_script.py"


def test_run_command_runs_the_uploaded_script(make_agent) -> None:
    arguments = _arguments(make_agent())

    assert arguments[:2] == ["python3", "/usr/local/fake_script.py"]


def test_run_command_uses_the_trial_scoped_proxy_url(make_agent) -> None:
    arguments = _arguments(make_agent())

    assert _value(arguments, "--base-url") == _PROXY_URL


def test_run_command_uses_the_default_behavior(make_agent) -> None:
    arguments = _arguments(make_agent())

    assert _value(arguments, "--requests") == "1"
    assert _value(arguments, "--exit-code") == "0"
    assert _value(arguments, "--sleep-s") == "0"
    assert _value(arguments, "--keep-requesting-s") == "0"
    assert "--solve" in arguments
    assert "--ignore-stop" not in arguments


@pytest.mark.parametrize(
    ("behavior", "flag", "expected"),
    [
        ({"requests": 4}, "--requests", "4"),
        ({"exit_code": 3}, "--exit-code", "3"),
        ({"sleep_s": 2.5}, "--sleep-s", "2.5"),
        ({"keep_requesting_s": 1.5}, "--keep-requesting-s", "1.5"),
    ],
    ids=["requests", "exit_code", "sleep_s", "keep_requesting_s"],
)
def test_run_command_uses_each_behavior_setting(
    make_agent, behavior: dict, flag: str, expected: str
) -> None:
    arguments = _arguments(make_agent(fake=behavior))

    assert _value(arguments, flag) == expected


def test_run_command_adds_ignore_stop_when_requested(make_agent) -> None:
    arguments = _arguments(make_agent(fake={"ignore_stop": True}))

    assert "--ignore-stop" in arguments


def test_run_command_omits_solve_when_disabled(make_agent) -> None:
    arguments = _arguments(make_agent(fake={"solve": False}))

    assert "--solve" not in arguments


def test_preflight_accepts_the_fake_settings() -> None:
    AgentFactory.run_preflight(_agent_config(_settings(fake={"requests": 2})))


def test_preflight_rejects_an_unknown_fake_behavior_field() -> None:
    with pytest.raises(ValueError, match="fake.bogus"):
        AgentFactory.run_preflight(_agent_config(_settings(fake={"bogus": 1})))


@pytest.mark.parametrize("behavior", [{"requests": -1}, {"sleep_s": -1}, {"keep_requesting_s": -1}])
def test_preflight_rejects_negative_counts_and_times(behavior: dict) -> None:
    with pytest.raises(ValueError):
        AgentFactory.run_preflight(_agent_config(_settings(fake=behavior)))


def test_construction_rejects_an_unknown_fake_behavior_field(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fake.bogus"):
        FakeHarnessAgent(logs_dir=tmp_path, model_name=_MODEL, **_settings(fake={"bogus": 1}))

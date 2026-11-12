from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

import pytest
from harbor.agents.factory import AgentFactory
from harbor.models.trial.config import AgentConfig

from harbor_research_runner.harness.agents import (
    InstallCorruptionError,
    OpenCodeAgent,
    PinnedHarness,
    ProbeError,
    agent_import_path,
    agent_settings,
)
from harbor_research_runner.harness.manifest import ManifestError
from harbor_research_runner.harness.opencode_eras import HarnessFacts

_COMMIT = "a" * 40
_DIGEST = "sha256:" + "b" * 64
_MODEL = "ahe/mimo-v2.5"
_PROXY_URL = "http://host.docker.internal:8766/trial/fix-git__Ab3dE9x/v1"
_AGENT_IMPORT_PATH = "harbor_research_runner.harness.agents:OpenCodeAgent"
_HELP_GLOBAL = "-m, --model  model to use\n"
_HELP_RUN = "--thinking\n--dangerously-skip-permissions\n"

_ENV = {
    "OPENCODE_FAKE_VCS": "git",
    "XDG_DATA_HOME": "/logs/agent/opencode/xdg-data",
    "XDG_STATE_HOME": "/logs/agent/opencode/xdg-state",
}

_CARD = {
    "id": "opencodego/mimo-v2.5-pro",
    "upstream_model": "mimo-v2.5",
    "variants": {
        "low": {"reasoningEffort": "low"},
        "high": {"reasoningEffort": "high"},
    },
    "default_variant": "high",
}

_OLD_STOP_SCRIPT = """\
read_ppid() {
  ppid=""
  line=$(cat "$1" 2>/dev/null) || return 1
  line=${line##*) }
  set -- $line
  ppid=$2
  [ -n "$ppid" ] || return 1
}
me=$$
self_tree=" $me "
probe=$me
while :; do
  read_ppid "/proc/$probe/stat" || break
  case "$ppid" in ""|0) break ;; esac
  case " $self_tree " in *" $ppid "*) break ;; esac
  self_tree="$self_tree $ppid"
  probe=$ppid
done
killed=0
signalled=" "
survivors=0
attempt=0
while [ "$attempt" -lt 10 ]; do
  targets=""
  for dir in /proc/[0-9]*; do
    pid=${dir#/proc/}
    case " $self_tree " in *" $pid "*) continue ;; esac
    [ -r "$dir/cmdline" ] || continue
    cmd=$(cat "$dir/cmdline" 2>/dev/null)
    case "$cmd" in
      *opencode*) targets="$targets $pid" ;;
    esac
  done
  changed=1
  while [ "$changed" = 1 ]; do
    changed=0
    for dir in /proc/[0-9]*; do
      pid=${dir#/proc/}
      case " $self_tree " in *" $pid "*) continue ;; esac
      case " $targets " in *" $pid "*) continue ;; esac
      [ -r "$dir/stat" ] || continue
      read_ppid "$dir/stat" || continue
      case " $targets " in
        *" $ppid "*) targets="$targets $pid"; changed=1 ;;
      esac
    done
  done
  for pid in $targets; do
    case "$signalled" in
      *" $pid "*) ;;
      *) if kill -KILL "$pid" 2>/dev/null; then killed=$((killed + 1)); signalled="$signalled$pid "; fi ;;
    esac
  done
  sleep 0.2
  survivors=0
  for pid in $targets; do
    [ -d "/proc/$pid" ] && survivors=$((survivors + 1))
  done
  [ "$survivors" -eq 0 ] && break
  attempt=$((attempt + 1))
done
printf 'killed=%s survivors=%s\\n' "$killed" "$survivors"
"""


class _Result:
    def __init__(self, return_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeEnvironment:
    def __init__(self, *, block_on: str | None = None) -> None:
        self.calls: list[dict] = []
        self.uploads: list[tuple[Path, str]] = []
        self.entered = asyncio.Event()
        self._block_on = block_on

    async def exec(self, command: str, **kwargs):
        self.calls.append({"command": command, **kwargs})
        if self._block_on is not None and self._block_on in command:
            self.entered.set()
            await asyncio.Event().wait()
        if command == "uname -m":
            return _Result(0, "x86_64\n")
        if command.endswith("opencode --version"):
            return _Result(0, "1.18.25\n")
        if command.endswith("opencode run --help"):
            return _Result(0, _HELP_RUN)
        if command.endswith("opencode --help"):
            return _Result(0, _HELP_GLOBAL)
        return _Result()

    async def upload_dir(self, source_dir: Path, target_dir: str) -> None:
        self.uploads.append((source_dir, target_dir))


class _TestAgent(OpenCodeAgent):
    def harness_files(self) -> Path:
        return self.files_root


@pytest.fixture
def manifest_path(tmp_path: Path) -> Path:
    path = tmp_path / "harness-manifest.toml"
    path.write_text(
        f'[opencode."{_COMMIT}"]\n'
        'version = "1.18.25"\n'
        "withdrawn = false\n"
        f'[opencode."{_COMMIT}".images.amd64]\n'
        'ref = "hrr/opencode:aaaaaaaaaaaa-amd64"\n'
        f'digest = "{_DIGEST}"\n'
        'build_method = "source"\n'
        'built_at = "2026-12-01"\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def make_agent(tmp_path: Path, manifest_path: Path):
    def make(*, variant: str | None = None, card: dict | None = None, **extra) -> _TestAgent:
        settings = agent_settings(
            arm_label="oc-1.18",
            harness="opencode",
            commit=_COMMIT,
            manifest_path=str(manifest_path),
            container_host="host.docker.internal",
            proxy_port=8766,
            variant=variant,
            model_card=dict(card) if card is not None else dict(_CARD),
        )
        agent = _TestAgent(logs_dir=tmp_path, model_name=_MODEL, **settings, **extra)
        agent.files_root = tmp_path / "harness-root"
        agent.files_root.mkdir(exist_ok=True)
        agent.session_id = "fix-git__Ab3dE9x__agent"
        return agent

    return make


def _facts() -> HarnessFacts:
    return HarnessFacts(version="1.18.25", help_text=_HELP_GLOBAL + _HELP_RUN, era="modern-v1")


def _settings(manifest_path: Path) -> dict:
    return agent_settings(
        arm_label="oc-1.18",
        harness="opencode",
        commit=_COMMIT,
        manifest_path=str(manifest_path),
        container_host="host.docker.internal",
        proxy_port=8766,
        variant=None,
        model_card=dict(_CARD),
    )


def _agent_config(settings: dict) -> AgentConfig:
    return AgentConfig(import_path=_AGENT_IMPORT_PATH, model_name=_MODEL, kwargs=settings)


def _exec_env(environment: _FakeEnvironment, marker: str) -> dict:
    calls = [call for call in environment.calls if marker in call["command"]]
    assert len(calls) == 1
    return calls[0]["env"]


def _config_payload(command: str) -> dict:
    segment = command.split("printf '%s' ", 1)[1].rsplit(" > ", 1)[0]
    return json.loads(shlex.split(segment)[0])


def test_stop_script_for_opencode_equals_the_old_script() -> None:
    assert PinnedHarness.stop_script("opencode") == _OLD_STOP_SCRIPT


def test_stop_script_uses_the_requested_pattern() -> None:
    script = PinnedHarness.stop_script("fake")

    assert "*fake*" in script
    assert "*opencode*" not in script


def test_trial_name_strips_the_agent_suffix(make_agent) -> None:
    assert make_agent().trial_name() == "fix-git__Ab3dE9x"


def test_trial_name_without_the_agent_suffix_raises(make_agent) -> None:
    agent = make_agent()
    agent.session_id = "fix-git__Ab3dE9x"

    with pytest.raises(ValueError, match="__agent"):
        agent.trial_name()


def test_proxy_base_url_uses_the_trial_name(make_agent) -> None:
    assert make_agent().proxy_base_url() == _PROXY_URL


def test_version_command_uses_the_pinned_binary(make_agent) -> None:
    assert make_agent().get_version_command() == "/usr/local/bin/opencode --version"


def test_agent_settings_returns_exactly_the_declared_keys(manifest_path: Path) -> None:
    settings = agent_settings(
        arm_label="oc-1.18",
        harness="opencode",
        commit=_COMMIT,
        manifest_path=str(manifest_path),
        container_host="host.docker.internal",
        proxy_port=8766,
        variant="low",
        model_card=dict(_CARD),
    )

    assert set(settings) == {
        "arm_label",
        "harness",
        "commit",
        "manifest_path",
        "container_host",
        "proxy_port",
        "variant",
        "model_card",
    }
    assert settings["model_card"] == _CARD


def test_agent_settings_adds_fake_for_the_fake_harness(manifest_path: Path) -> None:
    settings = agent_settings(
        arm_label="fake",
        harness="fake",
        commit=_COMMIT,
        manifest_path=str(manifest_path),
        container_host="host.docker.internal",
        proxy_port=8766,
        variant=None,
        model_card=dict(_CARD),
        fake={"requests": 2},
    )

    assert set(settings) == {
        "arm_label",
        "harness",
        "commit",
        "manifest_path",
        "container_host",
        "proxy_port",
        "variant",
        "model_card",
        "fake",
    }
    assert settings["fake"] == {"requests": 2}


def test_agent_settings_omits_fake_when_not_supplied(manifest_path: Path) -> None:
    settings = agent_settings(
        arm_label="fake",
        harness="fake",
        commit=_COMMIT,
        manifest_path=str(manifest_path),
        container_host="host.docker.internal",
        proxy_port=8766,
        variant=None,
        model_card=dict(_CARD),
    )

    assert set(settings) == {
        "arm_label",
        "harness",
        "commit",
        "manifest_path",
        "container_host",
        "proxy_port",
        "variant",
        "model_card",
    }


def test_agent_settings_rejects_fake_for_a_real_harness(manifest_path: Path) -> None:
    with pytest.raises(ValueError, match="fake"):
        agent_settings(
            arm_label="oc-1.18",
            harness="opencode",
            commit=_COMMIT,
            manifest_path=str(manifest_path),
            container_host="host.docker.internal",
            proxy_port=8766,
            variant=None,
            model_card=dict(_CARD),
            fake={"requests": 2},
        )


def test_agent_settings_requires_a_model_card(manifest_path: Path) -> None:
    with pytest.raises(TypeError):
        agent_settings(
            arm_label="oc-1.18",
            harness="opencode",
            commit=_COMMIT,
            manifest_path=str(manifest_path),
            container_host="host.docker.internal",
            proxy_port=8766,
            variant=None,
        )


def test_agent_settings_are_accepted_by_parse_options(manifest_path: Path) -> None:
    options = OpenCodeAgent.parse_options(_settings(manifest_path))

    assert options is not None
    assert options.arm_label == "oc-1.18"
    assert options.proxy_port == 8766
    assert options.model_card == _CARD


def test_agent_settings_pass_harbor_preflight(manifest_path: Path) -> None:
    AgentFactory.run_preflight(_agent_config(_settings(manifest_path)))


def test_preflight_rejects_an_unknown_setting(manifest_path: Path) -> None:
    settings = _settings(manifest_path)
    settings["bogus_setting"] = 1

    with pytest.raises(ValueError, match="Unknown option"):
        AgentFactory.run_preflight(_agent_config(settings))


def test_preflight_rejects_a_missing_setting(manifest_path: Path) -> None:
    settings = _settings(manifest_path)
    settings.pop("proxy_port")

    with pytest.raises(ValueError, match="proxy_port"):
        AgentFactory.run_preflight(_agent_config(settings))


def test_preflight_rejects_a_wrong_setting_type(manifest_path: Path) -> None:
    settings = _settings(manifest_path)
    settings["proxy_port"] = "nope"

    with pytest.raises(ValueError, match="valid integer"):
        AgentFactory.run_preflight(_agent_config(settings))


def test_construction_rejects_a_wrong_setting_type(tmp_path: Path, manifest_path: Path) -> None:
    settings = _settings(manifest_path)
    settings["proxy_port"] = "nope"

    with pytest.raises(ValueError, match="valid integer"):
        _TestAgent(logs_dir=tmp_path, model_name=_MODEL, **settings)


def test_construction_does_not_read_the_manifest(tmp_path: Path) -> None:
    missing = tmp_path / "missing-manifest.toml"

    agent = OpenCodeAgent(logs_dir=tmp_path, model_name=_MODEL, **_settings(missing))

    assert agent.commit == _COMMIT


def test_install_reads_the_manifest_before_uploading(tmp_path: Path) -> None:
    missing = tmp_path / "missing-manifest.toml"
    agent = OpenCodeAgent(logs_dir=tmp_path, model_name=_MODEL, **_settings(missing))
    environment = _FakeEnvironment()

    with pytest.raises(ManifestError, match="could not read harness manifest"):
        asyncio.run(agent.install(environment))

    assert environment.uploads == []


def test_agent_settings_round_trips_through_the_agent(make_agent) -> None:
    agent = make_agent(variant="low")

    assert agent.arm_label == "oc-1.18"
    assert agent.harness == "opencode"
    assert agent.commit == _COMMIT
    assert agent.container_host == "host.docker.internal"
    assert agent.proxy_port == 8766
    assert agent.variant == "low"
    assert agent.model_card == _CARD


def test_agent_settings_round_trip_carries_model_card_and_variant(make_agent) -> None:
    agent = make_agent(variant="low")

    config = json.loads(
        agent.config_files(_facts(), _MODEL, _PROXY_URL)["~/.config/opencode/opencode.json"]
    )

    assert config["model"] == "ahe/mimo-v2.5"
    assert config["provider"]["ahe"]["models"]["mimo-v2.5"]["variants"] == _CARD["variants"]
    assert "--variant low " in agent.run_command(_facts(), _MODEL, "do the thing")


def test_unknown_settings_key_raises(make_agent) -> None:
    with pytest.raises(ValueError, match="Unknown option"):
        make_agent(bogus_setting=1)


def test_missing_setting_raises(tmp_path: Path, manifest_path: Path) -> None:
    settings = agent_settings(
        arm_label="oc-1.18",
        harness="opencode",
        commit=_COMMIT,
        manifest_path=str(manifest_path),
        container_host="host.docker.internal",
        proxy_port=8766,
        variant=None,
        model_card=dict(_CARD),
    )
    settings.pop("proxy_port")

    with pytest.raises(ValueError, match="proxy_port"):
        _TestAgent(logs_dir=tmp_path, model_name=_MODEL, **settings)


def test_config_files_use_the_model_card_variants(make_agent) -> None:
    agent = make_agent()

    payload = json.loads(
        agent.config_files(_facts(), _MODEL, _PROXY_URL)["~/.config/opencode/opencode.json"]
    )
    entry = payload["provider"]["ahe"]["models"]["mimo-v2.5"]

    assert payload["model"] == _MODEL
    assert entry["variants"] == _CARD["variants"]
    assert entry["options"]["reasoningEffort"] == "high"


def test_config_files_use_the_trial_scoped_proxy_url(make_agent) -> None:
    agent = make_agent()

    payload = json.loads(
        agent.config_files(_facts(), _MODEL, _PROXY_URL)["~/.config/opencode/opencode.json"]
    )

    assert payload["provider"]["ahe"]["options"]["baseURL"] == _PROXY_URL


def test_run_command_uses_the_selected_variant(make_agent) -> None:
    agent = make_agent(variant="low")

    assert "--variant low " in agent.run_command(_facts(), _MODEL, "do the thing")


def test_run_command_uses_the_declared_default_when_variant_is_null(make_agent) -> None:
    agent = make_agent()

    assert "--variant high " in agent.run_command(_facts(), _MODEL, "do the thing")


def test_run_command_uses_the_run_scoped_model_flag_for_v2(make_agent) -> None:
    agent = make_agent(card={**_CARD, "default_variant": None})
    facts = HarnessFacts(version="2.0.32", help_text=_HELP_RUN, era="v2")

    command = agent.run_command(facts, _MODEL, "do the thing")

    assert "/usr/local/bin/opencode run --model=ahe/mimo-v2.5 --format=json " in command


def test_install_uploads_the_extracted_root_to_usr_local(make_agent) -> None:
    agent = make_agent()
    environment = _FakeEnvironment()

    asyncio.run(agent.install(environment))

    assert environment.uploads == [(agent.files_root, "/usr/local")]


def test_install_probes_the_task_architecture_first(make_agent) -> None:
    agent = make_agent()
    environment = _FakeEnvironment()

    asyncio.run(agent.install(environment))

    assert environment.calls[0]["command"] == "uname -m"


def test_install_probes_the_binary_version(make_agent) -> None:
    agent = make_agent()
    environment = _FakeEnvironment()

    asyncio.run(agent.install(environment))

    commands = [call["command"] for call in environment.calls]
    assert "/usr/local/bin/opencode --version" in commands


def test_install_writes_harness_facts_json(make_agent, tmp_path: Path) -> None:
    agent = make_agent()
    environment = _FakeEnvironment()

    asyncio.run(agent.install(environment))

    facts = json.loads((tmp_path / "harness-facts.json").read_text(encoding="utf-8"))
    assert facts["version"] == "1.18.25"
    assert facts["era"] == "modern-v1"
    assert "--thinking" in facts["help_text"]


def test_install_version_exit_126_raises_install_corruption(make_agent) -> None:
    agent = make_agent()

    class _CorruptEnvironment(_FakeEnvironment):
        async def exec(self, command: str, **kwargs):
            if command.endswith("opencode --version"):
                return _Result(126, "", "exec format error")
            return await super().exec(command, **kwargs)

    with pytest.raises(InstallCorruptionError, match="126"):
        asyncio.run(agent.install(_CorruptEnvironment()))


def test_install_version_failure_raises_probe_error(make_agent) -> None:
    agent = make_agent()

    class _FailingEnvironment(_FakeEnvironment):
        async def exec(self, command: str, **kwargs):
            if command.endswith("opencode --version"):
                return _Result(2, "", "boom")
            return await super().exec(command, **kwargs)

    with pytest.raises(ProbeError, match="--version"):
        asyncio.run(agent.install(_FailingEnvironment()))


def test_install_help_failure_raises_probe_error(make_agent) -> None:
    agent = make_agent()

    class _FailingEnvironment(_FakeEnvironment):
        async def exec(self, command: str, **kwargs):
            if command.endswith("opencode run --help"):
                return _Result(1, "", "boom")
            return await super().exec(command, **kwargs)

    with pytest.raises(ProbeError, match="run --help"):
        asyncio.run(agent.install(_FailingEnvironment()))


def test_install_upload_failure_raises_install_corruption(make_agent) -> None:
    agent = make_agent()

    class _FailingEnvironment(_FakeEnvironment):
        async def upload_dir(self, source_dir: Path, target_dir: str) -> None:
            raise ConnectionResetError("mid-copy")

    with pytest.raises(InstallCorruptionError, match="mid-copy"):
        asyncio.run(agent.install(_FailingEnvironment()))


def test_task_image_arch_exec_failure_raises_probe_error(make_agent) -> None:
    agent = make_agent()

    class _UnreachableEnvironment(_FakeEnvironment):
        async def exec(self, command: str, **kwargs):
            if command == "uname -m":
                raise ConnectionResetError("transport gone")
            return await super().exec(command, **kwargs)

    with pytest.raises(ProbeError, match="uname -m exec failed"):
        asyncio.run(agent._task_image_arch(_UnreachableEnvironment()))


def test_task_image_arch_unparsable_output_raises_probe_error(make_agent) -> None:
    agent = make_agent()

    class _UnparsableEnvironment(_FakeEnvironment):
        async def exec(self, command: str, **kwargs):
            if command == "uname -m":
                return _Result(0, "mips\n")
            return await super().exec(command, **kwargs)

    with pytest.raises(ProbeError, match="unparsable"):
        asyncio.run(agent._task_image_arch(_UnparsableEnvironment()))


@pytest.mark.parametrize(
    ("uname_out", "expected"),
    [("x86_64", "amd64"), ("amd64", "amd64"), ("aarch64", "arm64"), ("arm64", "arm64")],
    ids=["x86_64", "amd64", "aarch64", "arm64"],
)
def test_task_image_arch_maps_uname_output(make_agent, uname_out: str, expected: str) -> None:
    agent = make_agent()

    class _UnameEnvironment(_FakeEnvironment):
        async def exec(self, command: str, **kwargs):
            if command == "uname -m":
                return _Result(0, f"{uname_out}\n")
            return await super().exec(command, **kwargs)

    assert asyncio.run(agent._task_image_arch(_UnameEnvironment())) == expected


def test_probe_records_version_help_and_era(make_agent) -> None:
    agent = make_agent()

    facts = asyncio.run(agent.probe(_FakeEnvironment()))

    assert facts.version == "1.18.25"
    assert facts.era == "modern-v1"
    assert _HELP_GLOBAL in facts.help_text
    assert _HELP_RUN in facts.help_text


def test_run_writes_the_rendered_config_and_the_run_command(make_agent) -> None:
    agent = make_agent()
    environment = _FakeEnvironment()

    asyncio.run(agent.install(environment))
    asyncio.run(agent.run("write fizz buzz", environment, None))

    commands = [call["command"] for call in environment.calls]
    config_command = next(command for command in commands if "opencode.json" in command)
    payload = _config_payload(config_command)
    assert payload["model"] == _MODEL
    assert payload["provider"]["ahe"]["options"]["baseURL"] == _PROXY_URL
    assert any(
        "/usr/local/bin/opencode --model=ahe/mimo-v2.5 run --format=json " in command
        for command in commands
    )


def test_run_executes_the_agent_command_with_the_fixed_environment(make_agent) -> None:
    agent = make_agent()
    environment = _FakeEnvironment()

    asyncio.run(agent.install(environment))
    asyncio.run(agent.run("write fizz buzz", environment, None))

    assert _exec_env(environment, "--model=ahe/mimo-v2.5 run") == _ENV


def test_run_registers_config_with_the_fixed_environment(make_agent) -> None:
    agent = make_agent()
    environment = _FakeEnvironment()

    asyncio.run(agent.install(environment))
    asyncio.run(agent.run("write fizz buzz", environment, None))

    assert _exec_env(environment, "opencode.json") == _ENV


def test_run_registers_skills_with_the_fixed_environment(make_agent) -> None:
    agent = make_agent(skills_dir="/skills")
    environment = _FakeEnvironment()

    asyncio.run(agent.install(environment))
    asyncio.run(agent.run("write fizz buzz", environment, None))

    assert _exec_env(environment, "opencode/skills") == _ENV


def test_run_does_not_forward_unrelated_secret_environment(make_agent) -> None:
    agent = make_agent(extra_env={"OPENAI_API_KEY": "sk-do-not-forward"})
    environment = _FakeEnvironment()

    asyncio.run(agent.install(environment))
    asyncio.run(agent.run("write fizz buzz", environment, None))

    for call in environment.calls:
        env = call.get("env") or {}
        assert "OPENAI_API_KEY" not in env
        assert "sk-do-not-forward" not in env.values()


def test_run_without_install_raises_probe_error(make_agent) -> None:
    agent = make_agent()

    with pytest.raises(ProbeError, match="install"):
        asyncio.run(agent.run("write fizz buzz", _FakeEnvironment(), None))


def test_run_cancelled_executes_the_stop_script_once_and_reraises(make_agent) -> None:
    agent = make_agent()
    environment = _FakeEnvironment(block_on="/usr/local/bin/opencode --model")
    asyncio.run(agent.install(environment))

    async def _run() -> None:
        task = asyncio.create_task(agent.run("write fizz buzz", environment, None))
        await asyncio.wait_for(environment.entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())

    stop_calls = [
        call
        for call in environment.calls
        if call["command"] == PinnedHarness.stop_script("opencode")
    ]
    assert len(stop_calls) == 1
    assert stop_calls[0]["user"] == "root"
    assert stop_calls[0]["timeout_sec"] == 30


def test_run_cancelled_reraises_when_the_stop_script_fails(make_agent) -> None:
    agent = make_agent()

    class _FailingStopEnvironment(_FakeEnvironment):
        async def exec(self, command: str, **kwargs):
            if command == PinnedHarness.stop_script("opencode"):
                raise RuntimeError("docker exec client gone")
            return await super().exec(command, **kwargs)

    failing = _FailingStopEnvironment(block_on="/usr/local/bin/opencode --model")
    asyncio.run(agent.install(failing))

    async def _run() -> None:
        task = asyncio.create_task(agent.run("write fizz buzz", failing, None))
        await asyncio.wait_for(failing.entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())


def test_agent_import_path_maps_each_supported_harness() -> None:
    assert agent_import_path("opencode") == _AGENT_IMPORT_PATH
    assert agent_import_path("fake") == "harbor_research_runner.harness.fake:FakeHarnessAgent"


def test_agent_import_path_refuses_unknown_harness() -> None:
    with pytest.raises(ValueError, match="unknown harness"):
        agent_import_path("other")

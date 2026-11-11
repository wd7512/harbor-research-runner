"""The pinned-harness base every harness shares, and the OpenCode implementation."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, ClassVar

from harbor.agents.installed.base import NonZeroAgentExitCodeError, with_prompt_template
from harbor.agents.installed.opencode import OpenCode, OpenCodeOptions
from pydantic import BaseModel, Field

from harbor_research_runner.harness import opencode_eras
from harbor_research_runner.harness.extract import extract
from harbor_research_runner.harness.manifest import load_manifest, verify_arm
from harbor_research_runner.harness.opencode_eras import HarnessFacts
from harbor_research_runner.records.proxy_control import trial_base_url

logger = logging.getLogger(__name__)

_UPLOAD_TARGET = "/usr/local"
_OPENCODE_BIN = "/usr/local/bin/opencode"
_UNAME_COMMAND = "uname -m"
_HARNESS_FACTS_FILENAME = "harness-facts.json"
_AGENT_SUFFIX = "__agent"
_HARNESS_DIR_PARTS = (".cache", "harbor-research-runner", "harness")

_STOP_PPID_FRAGMENT = """\
read_ppid() {
  ppid=""
  line=$(cat "$1" 2>/dev/null) || return 1
  line=${line##*) }
  set -- $line
  ppid=$2
  [ -n "$ppid" ] || return 1
}
"""

_STOP_BODY_BEFORE_PATTERN = """\
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
      *"""

_STOP_BODY_AFTER_PATTERN = """*) targets="$targets $pid" ;;
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


class HarnessError(RuntimeError):
    """A harness could not be installed, probed or started."""


class ProbeError(HarnessError):
    """A capability, version or architecture probe failed or came back unknown."""


class InstallCorruptionError(HarnessError):
    """The uploaded harness binary or its layout is corrupt."""


class AgentSettings(BaseModel):
    arm_label: str = Field(min_length=1)
    harness: str = Field(min_length=1)
    commit: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)
    container_host: str = Field(min_length=1)
    proxy_port: int
    variant: str | None
    model_card: dict


_SETTINGS_KEYS = tuple(AgentSettings.model_fields)


class OpenCodeHarnessOptions(OpenCodeOptions, AgentSettings):
    pass


def agent_settings(
    arm_label: str,
    harness: str,
    commit: str,
    manifest_path: str,
    container_host: str,
    proxy_port: int,
    variant: str | None,
    *,
    model_card: dict,
    fake: dict | None = None,
) -> dict:
    """The Harbor agent kwargs for one arm and model."""
    settings = AgentSettings(
        arm_label=arm_label,
        harness=harness,
        commit=commit,
        manifest_path=manifest_path,
        container_host=container_host,
        proxy_port=proxy_port,
        variant=variant,
        model_card=model_card,
    ).model_dump()
    if fake is not None:
        if harness != "fake":
            raise ValueError("fake settings require the fake harness")
        settings["fake"] = fake
    return settings


def _parse_agent_settings(values: dict) -> AgentSettings:
    missing = [key for key in _SETTINGS_KEYS if key not in values]
    if missing:
        raise ValueError(f"missing agent setting(s): {', '.join(missing)}")
    unknown = sorted(set(values) - set(_SETTINGS_KEYS))
    if unknown:
        raise ValueError(f"unknown agent setting(s): {', '.join(unknown)}")
    settings = AgentSettings.model_validate(values)
    model_card = settings.model_card
    if not isinstance(model_card.get("upstream_model"), str) or not model_card["upstream_model"]:
        raise ValueError("agent setting 'model_card' must carry upstream_model")
    if not isinstance(model_card.get("variants"), dict):
        raise ValueError("agent setting 'model_card' variants must be a mapping")
    default_variant = model_card.get("default_variant")
    if default_variant is not None and not isinstance(default_variant, str):
        raise ValueError("agent setting 'model_card' default_variant must be a string or null")
    return settings


def _extraction_workdir() -> Path:
    return Path.home().joinpath(*_HARNESS_DIR_PARTS)


def _uname_to_arch(uname_out: str) -> str | None:
    return {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(uname_out)


class PinnedHarness(ABC):
    """A harness whose binary comes from a pinned image on the host."""

    harness_name: ClassVar[str]
    session_id: str | None
    logs_dir: Path
    exec_as_agent: Callable[..., Awaitable[Any]]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        settings = _parse_agent_settings(
            {key: kwargs[key] for key in _SETTINGS_KEYS if key in kwargs}
        )
        self.arm_label = settings.arm_label
        self.harness = settings.harness
        self.commit = settings.commit
        self.manifest_path = settings.manifest_path
        self.container_host = settings.container_host
        self.proxy_port = settings.proxy_port
        self.variant = settings.variant
        self.model_card = settings.model_card
        self._task_arch: str | None = None
        self._facts: HarnessFacts | None = None

    @abstractmethod
    def config_files(
        self, facts: HarnessFacts, model: str, proxy_base_url: str
    ) -> dict[str, str]: ...

    @abstractmethod
    def run_command(self, facts: HarnessFacts, model: str, instruction: str) -> str: ...

    @abstractmethod
    async def probe(self, environment) -> HarnessFacts: ...

    @abstractmethod
    def process_pattern(self) -> str: ...

    @staticmethod
    def stop_script(pattern: str) -> str:
        """POSIX shell that kills the agent tree inside the task container."""
        return _STOP_PPID_FRAGMENT + _STOP_BODY_BEFORE_PATTERN + pattern + _STOP_BODY_AFTER_PATTERN

    def trial_name(self) -> str:
        session_id = self.session_id
        if not session_id or not session_id.endswith(_AGENT_SUFFIX):
            raise ValueError(f"session id {session_id!r} does not end with {_AGENT_SUFFIX!r}")
        return session_id[: -len(_AGENT_SUFFIX)]

    def proxy_base_url(self) -> str:
        return trial_base_url(self.container_host, self.proxy_port, self.trial_name())

    def harness_files(self) -> Path:
        if self._task_arch is None:
            raise ProbeError("task architecture probe missing; install() must run first")
        manifest = load_manifest(Path(self.manifest_path))
        entry = verify_arm(manifest, self.harness, self.commit, self._task_arch)
        return extract(entry, workdir=_extraction_workdir()).local_root

    async def install(self, environment) -> None:
        self._task_arch = await self._task_image_arch(environment)
        local_root = self.harness_files()
        await self._upload_harness(environment, local_root)
        self._facts = await self.probe(environment)
        self._persist_harness_facts(self._facts)

    async def run(self, instruction, environment, context) -> None:
        facts = self._facts
        if facts is None:
            raise ProbeError(
                f"capability probe missing for {self.harness}:{self.commit}; "
                "install() must run before run()"
            )
        model = self._render_model_name()
        env = self._run_environment()
        for path, text in self.config_files(facts, model, self.proxy_base_url()).items():
            await self.exec_as_agent(environment, command=_write_file_command(path, text), env=env)
        try:
            await self.exec_as_agent(
                environment, command=self.run_command(facts, model, instruction), env=env
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._stop_agent_processes(environment))
            raise

    def _run_environment(self) -> dict[str, str]:
        return {}

    async def _upload_harness(self, environment, local_root: Path) -> None:
        try:
            await environment.upload_dir(local_root, _UPLOAD_TARGET)
        except Exception as exc:
            raise InstallCorruptionError(
                f"upload_dir mid-copy failure for {self.harness}:{self.commit}: {exc}"
            ) from exc

    async def _task_image_arch(self, environment) -> str:
        try:
            result = await environment.exec(command=_UNAME_COMMAND)
        except Exception as exc:
            raise ProbeError(f"arch probe: {_UNAME_COMMAND} exec failed: {exc}") from exc
        if result.return_code != 0:
            raise ProbeError(
                f"arch probe: {_UNAME_COMMAND} exited {result.return_code}: "
                f"stdout={(result.stdout or '').strip()!r} "
                f"stderr={(result.stderr or '').strip()!r}"
            )
        output = (result.stdout or "").strip()
        mapped = _uname_to_arch(output)
        if mapped is None:
            raise ProbeError(f"arch probe: unparsable uname -m output {output!r}")
        return mapped

    async def _stop_agent_processes(self, environment) -> None:
        script = self.stop_script(self.process_pattern())
        try:
            result = await environment.exec(command=script, user="root", timeout_sec=30)
        except Exception as exc:
            logger.warning("agent stop script could not run: %s", exc)
            return
        logger.warning(
            "agent stop script: rc=%s stdout=%r stderr=%r",
            result.return_code,
            (result.stdout or "").strip(),
            (result.stderr or "").strip(),
        )

    def _persist_harness_facts(self, facts: HarnessFacts) -> None:
        try:
            facts_path = Path(self.logs_dir) / _HARNESS_FACTS_FILENAME
            facts_path.write_text(json.dumps(asdict(facts), indent=2), encoding="utf-8")
            logger.info("harness facts written: %s", facts_path)
        except OSError as exc:
            logger.warning("could not write %s (non-fatal): %s", _HARNESS_FACTS_FILENAME, exc)

    def _render_model_name(self) -> str:
        return f"ahe/{self.model_card['upstream_model']}"

    def _default_variant(self) -> str | None:
        default_variant = self.model_card.get("default_variant")
        return default_variant if isinstance(default_variant, str) else None

    def _selected_variant(self) -> str | None:
        if self.variant is not None:
            return self.variant
        return self._default_variant()


def _write_file_command(path: str, text: str) -> str:
    command = f"printf '%s' {shlex.quote(text)} > {path}"
    parent, _separator, _name = path.rpartition("/")
    if parent:
        command = f"mkdir -p {parent} && {command}"
    return command


def _probe_failure_message(ref: str, what: str, result: Any) -> str:
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    return (
        f"{what} in-container for {ref!r} (exit {result.return_code}): "
        f"stdout={stdout!r} stderr={stderr!r}"
    )


class OpenCodeAgent(PinnedHarness, OpenCode):
    """The OpenCode harness, loaded by Harbor from a pinned image."""

    harness_name = "opencode"
    options_model = OpenCodeHarnessOptions

    def config_files(self, facts: HarnessFacts, model: str, proxy_base_url: str) -> dict[str, str]:
        return opencode_eras.config_files(
            facts,
            model,
            proxy_base_url,
            self._selected_variant(),
            variants=self.model_card.get("variants") or {},
            default_variant=self._default_variant(),
        )

    def run_command(self, facts: HarnessFacts, model: str, instruction: str) -> str:
        return opencode_eras.run_command(facts, model, instruction, self._selected_variant())

    async def probe(self, environment) -> HarnessFacts:
        version = await self._probe_version(environment)
        run_help = await self._probe_help(environment, f"{_OPENCODE_BIN} run --help")
        global_help = await self._probe_help(environment, f"{_OPENCODE_BIN} --help")
        return HarnessFacts(
            version=version,
            help_text=global_help + run_help,
            era=opencode_eras.era_for(version),
        )

    def process_pattern(self) -> str:
        return opencode_eras.PROCESS_PATTERN

    def _run_environment(self) -> dict[str, str]:
        return {
            "OPENCODE_FAKE_VCS": "git",
            "XDG_DATA_HOME": "/logs/agent/opencode/xdg-data",
            "XDG_STATE_HOME": "/logs/agent/opencode/xdg-state",
        }

    def get_version_command(self) -> str | None:
        return f"{_OPENCODE_BIN} --version"

    async def _probe_version(self, environment) -> str:
        result = await environment.exec(command=f"{_OPENCODE_BIN} --version")
        ref = f"{self.harness}:{self.commit}"
        if result.return_code == 126:
            raise InstallCorruptionError(
                _probe_failure_message(ref, "opencode binary corrupt/exec-format", result)
            )
        if result.return_code != 0:
            raise ProbeError(_probe_failure_message(ref, "opencode --version failed", result))
        return (result.stdout or "").strip()

    async def _probe_help(self, environment, command: str) -> str:
        result = await environment.exec(command=command)
        if result.return_code != 0:
            raise ProbeError(
                _probe_failure_message(f"{self.harness}:{self.commit}", f"{command} failed", result)
            )
        return result.stdout or ""

    @with_prompt_template
    async def run(self, instruction, environment, context) -> None:
        self._instruction = instruction
        skills_command = self._build_register_skills_command()
        if skills_command:
            await self.exec_as_agent(
                environment, command=skills_command, env=self._run_environment()
            )
        await super().run(instruction, environment, context)
        if messages := self._error_messages():
            raise NonZeroAgentExitCodeError(
                "OpenCode emitted error event(s): " + "; ".join(messages[:3])
            )

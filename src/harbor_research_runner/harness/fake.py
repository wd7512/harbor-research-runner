"""A scripted harness that costs nothing: no image, no manifest, fixed behaviour."""

from __future__ import annotations

import hashlib
import os
import shlex
import tempfile
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.agents.options import InstalledAgentOptions
from pydantic import BaseModel, ConfigDict, Field, field_validator

from harbor_research_runner.harness import agents
from harbor_research_runner.harness.agents import AgentSettings, PinnedHarness
from harbor_research_runner.harness.opencode_eras import HarnessFacts

_SCRIPT = "/usr/local/fake_script.py"
_PROCESS_PATTERN = "fake_script.py"


class FakeBehavior(BaseModel):
    """The typed behaviour an arm can ask the fake script to perform."""

    model_config = ConfigDict(extra="forbid")

    requests: int = Field(default=1, ge=0)
    exit_code: int = 0
    sleep_s: float = Field(default=0, ge=0)
    ignore_stop: bool = False
    keep_requesting_s: float = Field(default=0, ge=0)
    detached_heartbeat: str | None = None
    detached_requesting_s: float = Field(default=0, ge=0)
    continue_on_error: bool = False
    solve: bool = True

    @field_validator("detached_heartbeat")
    @classmethod
    def _absolute_heartbeat_path(cls, value: str | None) -> str | None:
        if value is not None and not Path(value).is_absolute():
            raise ValueError("detached_heartbeat must be an absolute container path")
        return value


class FakeHarnessOptions(InstalledAgentOptions, AgentSettings):
    """Harbor's preflight and the agent constructor parse this one dictionary."""

    model_config = ConfigDict(extra="forbid")

    fake: FakeBehavior | None = None


class FakeHarnessAgent(PinnedHarness, BaseInstalledAgent):
    """The fake harness: a standard-library script uploaded into the container."""

    harness_name = "fake"
    options_model = FakeHarnessOptions

    @staticmethod
    def name() -> str:
        return "fake"

    def __init__(self, *args, fake: dict | None = None, **kwargs) -> None:
        super().__init__(*args, fake=fake, **kwargs)
        options = self.options
        behavior = options.fake if isinstance(options, FakeHarnessOptions) else None
        self.fake = behavior if behavior is not None else FakeBehavior()

    def harness_files(self) -> Path:
        script = Path(__file__).with_name("fake_script.py").read_bytes()
        local_root = agents._extraction_workdir() / f"fake-{hashlib.sha256(script).hexdigest()}"
        target = local_root / "fake_script.py"
        if target.is_file() and target.read_bytes() == script:
            return local_root
        local_root.mkdir(parents=True, exist_ok=True)
        descriptor, staged = tempfile.mkstemp(dir=local_root, prefix=".fake_script-")
        with os.fdopen(descriptor, "wb") as staged_file:
            staged_file.write(script)
        os.replace(staged, target)
        return local_root

    def config_files(self, facts: HarnessFacts, model: str, proxy_base_url: str) -> dict[str, str]:
        return {}

    def run_command(self, facts: HarnessFacts, model: str, instruction: str) -> str:
        arguments = [
            "python3",
            _SCRIPT,
            "--base-url",
            self.proxy_base_url(),
            "--requests",
            str(self.fake.requests),
            "--exit-code",
            str(self.fake.exit_code),
            "--sleep-s",
            str(self.fake.sleep_s),
            "--keep-requesting-s",
            str(self.fake.keep_requesting_s),
            "--detached-requesting-s",
            str(self.fake.detached_requesting_s),
        ]
        if self.fake.detached_heartbeat is not None:
            arguments.extend(["--detached-heartbeat", self.fake.detached_heartbeat])
        if self.fake.continue_on_error:
            arguments.append("--continue-on-error")
        if self.fake.ignore_stop:
            arguments.append("--ignore-stop")
        if self.fake.solve:
            arguments.append("--solve")
        return shlex.join(arguments)

    async def probe(self, environment) -> HarnessFacts:
        return HarnessFacts(version="fake", help_text="", era="fake", global_help_text="")

    def process_pattern(self) -> str:
        return _PROCESS_PATTERN

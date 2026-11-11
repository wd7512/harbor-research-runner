import json
from dataclasses import dataclass, fields
from pathlib import Path

OPEN_PATH = "/control/open"
CLOSE_PATH = "/control/close"
HEALTH_PATH = "/control/health"
CLOSED_BODY = '{"error":{"message":"trial closed","type":"trial_closed"}}'


def trial_base_url(host: str, port: int, trial: str) -> str:
    return f"http://{host}:{port}/trial/{trial}/v1"


def trial_from_path(path: str) -> tuple[str, str] | None:
    prefix = "/trial/"
    if not path.startswith(prefix):
        return None
    trial, separator, remainder = path[len(prefix) :].partition("/")
    if not trial or not separator:
        return None
    return trial, f"/{remainder}"


@dataclass(frozen=True)
class OpenMessage:
    trial: str
    task: str
    arm: str

    def to_row(self) -> dict:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def to_json(self) -> str:
        return json.dumps(self.to_row(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_row(cls, row: dict) -> "OpenMessage":
        return _parse(cls, row)


@dataclass(frozen=True)
class CloseMessage:
    trial: str

    def to_row(self) -> dict:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def to_json(self) -> str:
        return json.dumps(self.to_row(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_row(cls, row: dict) -> "CloseMessage":
        return _parse(cls, row)


@dataclass(frozen=True)
class ProxyConfig:
    host: str
    port: int
    ledger_path: str
    bodies_dir: str | None
    model: dict
    limits: dict
    restart_cooldown_s: float

    def to_row(self) -> dict:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def to_json(self) -> str:
        return json.dumps(self.to_row(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_row(cls, row: dict) -> "ProxyConfig":
        return _parse(cls, row)


def write(path: Path, config: ProxyConfig) -> None:
    path.write_text(config.to_json(), encoding="utf-8")


def read(path: Path) -> ProxyConfig:
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid proxy config JSON") from exc
    if not isinstance(row, dict):
        raise ValueError("proxy config must be a JSON object")
    return ProxyConfig.from_row(row)


def _parse(record_type, row: dict):
    if set(row) != {item.name for item in fields(record_type)}:
        raise ValueError("proxy control fields do not match")
    try:
        return record_type(**row)
    except TypeError as exc:
        raise ValueError("invalid proxy control record") from exc

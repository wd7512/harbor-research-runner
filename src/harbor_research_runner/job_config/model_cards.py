from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelCard:
    id: str
    upstream_model: str
    api: str
    endpoints: list[str]
    key_env: list[str]
    tokens_per_minute: int | None
    pricing: dict[str, float]
    variants: dict
    withdrawn: bool
    reason: str | None


class ModelCardError(ValueError):
    pass


_CARD_ROOT = Path(__file__).parent / "model_cards"
_FIELDS = {
    "id",
    "upstream_model",
    "api",
    "endpoints",
    "key_env",
    "tokens_per_minute",
    "pricing",
    "variants",
    "withdrawn",
    "reason",
}


def _card_path(root: Path, model_id: str) -> Path:
    return root / f"{model_id}.json"


def load_card(model_id: str, extra_dir: Path | None = None) -> ModelCard:
    path = _card_path(_CARD_ROOT, model_id)
    if not path.is_file() and extra_dir is not None:
        path = _card_path(extra_dir, model_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelCardError(f"could not read model card {model_id!r}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ModelCardError(f"model card {model_id!r} must be a JSON object")
    unknown = sorted(set(raw) - _FIELDS)
    if unknown:
        raise ModelCardError(f"unknown model card key(s): {', '.join(unknown)}")
    missing = sorted(_FIELDS - set(raw))
    if missing:
        raise ModelCardError(f"model card missing required field(s): {', '.join(missing)}")
    if raw["id"] != model_id:
        raise ModelCardError(f"model card id {raw['id']!r} does not match {model_id!r}")
    if raw["api"] not in {"chat", "responses"}:
        raise ModelCardError("model card api must be chat or responses")
    if not isinstance(raw["endpoints"], list) or any(
        not isinstance(x, str) for x in raw["endpoints"]
    ):
        raise ModelCardError("model card endpoints must be a list of strings")
    if not isinstance(raw["key_env"], list) or any(not isinstance(x, str) for x in raw["key_env"]):
        raise ModelCardError("model card key_env must be a list of strings")
    if raw["tokens_per_minute"] is not None and (
        not isinstance(raw["tokens_per_minute"], int) or isinstance(raw["tokens_per_minute"], bool)
    ):
        raise ModelCardError("model card tokens_per_minute must be an integer or null")
    pricing = raw["pricing"]
    pricing_fields = {"input", "output", "cached_read"}
    if not isinstance(pricing, dict) or set(pricing) != pricing_fields:
        raise ModelCardError("model card pricing must have input, output, and cached_read")
    if any(
        not isinstance(value, (int, float)) or isinstance(value, bool) for value in pricing.values()
    ):
        raise ModelCardError("model card pricing values must be numbers")
    if not isinstance(raw["variants"], dict):
        raise ModelCardError("model card variants must be a mapping")
    if not isinstance(raw["withdrawn"], bool):
        raise ModelCardError("model card withdrawn must be a boolean")
    if raw["reason"] is not None and not isinstance(raw["reason"], str):
        raise ModelCardError("model card reason must be a string or null")
    if not isinstance(raw["upstream_model"], str) or not isinstance(raw["id"], str):
        raise ModelCardError("model card id and upstream_model must be strings")
    return ModelCard(**raw)

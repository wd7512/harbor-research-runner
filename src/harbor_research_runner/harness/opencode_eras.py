"""OpenCode version eras: config file text and run command as plain functions."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass

PROCESS_PATTERN = "opencode"

LEGACY_V1_0 = "legacy-v1.0"
MODERN_V1 = "modern-v1"
V2 = "v2"

_LEGACY_V1_0_RE = re.compile(r"^1\.0\.\d{1,2}(?:-[0-9a-z]+)?$")
_PROBED_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?(?:-[0-9a-z]+)?)")
_VERSION_MAJOR_RE = re.compile(r"^(\d+)\.")

_OPENCODE_BIN = "/usr/local/bin/opencode"
_AGENT_LOG_PATH = "/logs/agent/opencode.txt"
_CONFIG_PATH = "~/.config/opencode/opencode.json"

_TITLE_SUPPRESS_PROVIDER = "_title_suppress"
_TITLE_SUPPRESS_MODEL = "title-suppress"
_TITLE_SUPPRESS_BASE_URL = "http://192.0.2.1:1"
_TITLE_SUPPRESS_API_KEY = "noop"
_OPENAI_COMPATIBLE_PACKAGE = "@opencode/ai/providers/openai-compatible"

_TEMPERATURE = 0.6
_TOP_P = 0.95
_CONTEXT_LIMIT = 1_048_576
_OUTPUT_LIMIT = 384_000

_SAFE_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")


@dataclass(frozen=True)
class HarnessFacts:
    version: str
    help_text: str
    era: str
    global_help_text: str = ""


def era_for(version: str) -> str:
    """Classify an OpenCode version into its config and CLI era."""
    match = _PROBED_VERSION_RE.search(version)
    if match is None:
        return MODERN_V1
    probed = match.group(1)
    if _LEGACY_V1_0_RE.match(probed):
        return LEGACY_V1_0
    major = _VERSION_MAJOR_RE.match(probed)
    if major is not None and major.group(1) == "2":
        return V2
    return MODERN_V1


def supports_flag(help_text: str, flag: str) -> bool:
    """True iff the help output advertises *flag* as a token."""
    pattern = rf"(?<!\S){re.escape(flag)}(?=\s|=|,|$)"
    return re.search(pattern, help_text, re.MULTILINE) is not None


def config_files(
    facts: HarnessFacts,
    model: str,
    proxy_base_url: str,
    variant: str | None,
    *,
    variants: dict[str, dict] | None = None,
    default_variant: str | None = None,
) -> dict[str, str]:
    """The OpenCode config text to write, keyed by container path."""
    supplied_variants = variants or {}
    _require_known_variant(variant, supplied_variants, "selected")
    _require_known_variant(default_variant, supplied_variants, "default")
    if "/" not in model:
        return {}
    provider, model_id = model.split("/", 1)
    if facts.era == V2:
        config = _v2_config(provider, model_id, proxy_base_url, supplied_variants, default_variant)
    else:
        config = _v1_config(
            provider, model_id, proxy_base_url, facts.era, supplied_variants, default_variant
        )
    return {_CONFIG_PATH: json.dumps(config, indent=2)}


def run_command(facts: HarnessFacts, model: str, instruction: str, variant: str | None) -> str:
    """The in-container ``opencode run`` command for the probed capabilities."""
    _require_shell_safe_model_name(model)
    escaped_instruction = shlex.quote(instruction)
    model_value = model
    variant_flag = ""
    if facts.era == V2:
        if variant:
            model_value = f"{model}#{variant}"
    elif variant:
        variant_flag = f"--variant {shlex.quote(variant)} "
    model_arg = shlex.quote(f"--model={model_value}")
    thinking_flag = "--thinking " if supports_flag(facts.help_text, "--thinking") else ""
    skip_permissions_flag = (
        "--dangerously-skip-permissions "
        if supports_flag(facts.help_text, "--dangerously-skip-permissions")
        else ""
    )
    if supports_flag(facts.global_help_text, "--model"):
        invocation = f"{_OPENCODE_BIN} {model_arg} run --format=json "
    else:
        invocation = f"{_OPENCODE_BIN} run {model_arg} --format=json "
    return (
        "[ -f ~/.nvm/nvm.sh ] && . ~/.nvm/nvm.sh; "
        f"{invocation}{variant_flag}{thinking_flag}"
        f"{skip_permissions_flag}-- {escaped_instruction} "
        f"2>&1 </dev/null | stdbuf -oL tee {_AGENT_LOG_PATH}"
    )


def _v1_config(
    provider: str,
    model_id: str,
    proxy_base_url: str,
    era: str,
    variants: dict[str, dict],
    default_variant: str | None,
) -> dict[str, object]:
    provider_options: dict[str, object] = {
        "baseURL": proxy_base_url,
        "apiKey": "ahe",
    }
    if era == LEGACY_V1_0:
        provider_options["npm"] = "@ai-sdk/openai-compatible"
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"{provider}/{model_id}",
        "provider": {
            provider: {
                "options": provider_options,
                "models": {
                    model_id: _model_entry(
                        model_id, "options", variants, default_variant, cli_2x=False
                    )
                },
            },
            _TITLE_SUPPRESS_PROVIDER: {
                "options": {
                    "baseURL": _TITLE_SUPPRESS_BASE_URL,
                    "apiKey": _TITLE_SUPPRESS_API_KEY,
                },
                "models": {
                    _TITLE_SUPPRESS_MODEL: {
                        "name": _TITLE_SUPPRESS_MODEL,
                        "tool_call": False,
                        "limit": {"context": 1, "output": 1},
                    }
                },
            },
        },
        "permission": (
            {"edit": "allow", "bash": "allow", "webfetch": "allow"}
            if era == LEGACY_V1_0
            else "allow"
        ),
        "share": "disabled",
        "small_model": f"{_TITLE_SUPPRESS_PROVIDER}/{_TITLE_SUPPRESS_MODEL}",
    }


def _v2_config(
    provider: str,
    model_id: str,
    proxy_base_url: str,
    variants: dict[str, dict],
    default_variant: str | None,
) -> dict[str, object]:
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"{provider}/{model_id}",
        "providers": {
            provider: {
                "package": _OPENAI_COMPATIBLE_PACKAGE,
                "settings": {
                    "baseURL": proxy_base_url,
                    "apiKey": "ahe",
                },
                "models": {
                    model_id: _model_entry(
                        model_id, "settings", variants, default_variant, cli_2x=True
                    )
                },
            },
            _TITLE_SUPPRESS_PROVIDER: {
                "package": _OPENAI_COMPATIBLE_PACKAGE,
                "settings": {
                    "baseURL": _TITLE_SUPPRESS_BASE_URL,
                    "apiKey": _TITLE_SUPPRESS_API_KEY,
                },
                "models": {
                    _TITLE_SUPPRESS_MODEL: {
                        "name": _TITLE_SUPPRESS_MODEL,
                        "tool_call": False,
                        "limit": {"context": 1, "output": 1},
                    }
                },
            },
        },
        "permission": "allow",
        "share": "disabled",
        "small_model": f"{_TITLE_SUPPRESS_PROVIDER}/{_TITLE_SUPPRESS_MODEL}",
    }


def _model_entry(
    model_id: str,
    settings_key: str,
    variants: dict[str, dict],
    default_variant: str | None,
    *,
    cli_2x: bool,
) -> dict[str, object]:
    base: dict[str, object] = {"temperature": _TEMPERATURE, "top_p": _TOP_P}
    if default_variant is not None:
        base.update(variants[default_variant])
    entry: dict[str, object] = {
        "name": model_id,
        "tool_call": True,
        "limit": {"context": _CONTEXT_LIMIT, "output": _OUTPUT_LIMIT},
        settings_key: base,
    }
    if variants:
        if cli_2x:
            entry["variants"] = [
                {"id": variant_id, "settings": settings}
                for variant_id, settings in variants.items()
            ]
        else:
            entry["variants"] = dict(variants)
    return entry


def _require_known_variant(name: str | None, variants: dict[str, dict], kind: str) -> None:
    if name is not None and name not in variants:
        raise ValueError(f"{kind} variant {name!r} is not in the supplied variant map")


def _require_shell_safe_model_name(model: str) -> None:
    if "/" not in model or not _SAFE_MODEL_NAME_RE.match(model):
        raise ValueError(
            f"model_name {model!r} is not shell-safe; expected a provider/model id "
            "using only letters, digits, and . _ - : / @ +"
        )

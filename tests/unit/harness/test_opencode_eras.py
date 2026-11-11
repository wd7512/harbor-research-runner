from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from harbor_research_runner.harness.opencode_eras import (
    PROCESS_PATTERN,
    HarnessFacts,
    config_files,
    era_for,
    run_command,
    supports_flag,
)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "opencode_eras"
_MODEL = "ahe/mimo-v2.5"
_MODEL_ID = "mimo-v2.5"

_VARIANTS_A = {
    "low": {"reasoningEffort": "low"},
    "high": {"reasoningEffort": "high"},
}
_VARIANTS_B = {
    "fast": {"reasoningEffort": "none", "verbosity": "low"},
    "deep": {"reasoningEffort": "max", "verbosity": "high"},
}
_INSTRUCTION = "write fizz buzz"
_PROXY_BASE_URL = "http://host.docker.internal:8766/trial/trial-a/v1"
_CONFIG_PATH = "~/.config/opencode/opencode.json"

_HELP_1X = """\
opencode --help
-m, --model  model to use

opencode run --help
--thinking  show thinking blocks
"""

_HELP_V2 = """\
opencode --help
run-only model selection

opencode run --help
--thinking  show thinking blocks
--dangerously-skip-permissions  skip permission prompts
"""

_HELP_MODERN = """\
opencode run [message..]

Options:
      --thinking    show thinking blocks                                  [boolean] [default: false]
      --dangerously-skip-permissions  skip permission prompts             [boolean] [default: false]
"""

_HELP_LEGACY = """\
opencode run [message..]

Options:
      --thinking    show thinking blocks                                  [boolean] [default: false]
"""

_GOLDEN_CASES = [
    (
        "legacy-v1.0",
        "1.0.19",
        _HELP_1X,
        "legacy-v1.0",
    ),
    (
        "modern",
        "1.18.25",
        _HELP_1X,
        "modern-v1",
    ),
    (
        "modern-1.19.3",
        "1.19.3",
        _HELP_1X,
        "modern-v1",
    ),
    (
        "2x",
        "2.0.32",
        _HELP_V2,
        "v2",
    ),
]
_GOLDEN_IDS = ["legacy-v1.0", "modern", "modern-1.19.3", "2x"]


@pytest.mark.parametrize(
    ("stem", "version", "help_text", "era"),
    _GOLDEN_CASES,
    ids=_GOLDEN_IDS,
)
def test_era_config_and_command_match_examples(
    stem: str, version: str, help_text: str, era: str
) -> None:
    facts = HarnessFacts(version=version, help_text=help_text, era=era_for(version))

    assert facts.era == era
    assert config_files(facts, _MODEL, _PROXY_BASE_URL, None) == {
        _CONFIG_PATH: (_FIXTURES / f"{stem}.json").read_text(encoding="utf-8")
    }
    assert run_command(facts, _MODEL, _INSTRUCTION, None) == (_FIXTURES / f"{stem}.cmd").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.0.19", "legacy-v1.0"),
        ("1.0.19-amd64", "legacy-v1.0"),
        ("1.0.195", "modern-v1"),
        ("1.14.51", "modern-v1"),
        ("1.14.51-amd64", "modern-v1"),
        ("1.18.25-amd64", "modern-v1"),
        ("1.19.0", "modern-v1"),
        ("2.0.32", "v2"),
        ("opencode v2.0.32", "v2"),
    ],
    ids=[
        "legacy",
        "legacy-with-arch",
        "legacy-looking-modern",
        "modern",
        "modern-with-arch",
        "modern-with-arch-118",
        "modern-119",
        "v2",
        "v2-with-prefix",
    ],
)
def test_era_for_version_boundaries(version: str, expected: str) -> None:
    assert era_for(version) == expected


@pytest.mark.parametrize(
    ("help_text", "flag", "expected"),
    [
        (_HELP_MODERN, "--dangerously-skip-permissions", True),
        (_HELP_MODERN, "--thinking", True),
        (_HELP_LEGACY, "--dangerously-skip-permissions", False),
        (_HELP_LEGACY, "--thinking", True),
        ("", "--thinking", False),
        ("nothing here", "--thinking", False),
        ("--thinking  enable\n--dangerously-skip-permissions  yolo", "--thinking", True),
        (
            "--thinking  enable\n--dangerously-skip-permissions  yolo",
            "--dangerously-skip-permissions",
            True,
        ),
    ],
    ids=[
        "modern-skip",
        "modern-thinking",
        "legacy-skip-absent",
        "legacy-thinking",
        "empty",
        "missing",
        "probe-thinking",
        "probe-skip",
    ],
)
def test_supports_flag(help_text: str, flag: str, expected: bool) -> None:
    assert supports_flag(help_text, flag) is expected


def test_config_files_returns_empty_mapping_without_provider_model() -> None:
    facts = HarnessFacts(version="1.18.25", help_text=_HELP_1X, era="modern-v1")

    assert config_files(facts, "noprovider", _PROXY_BASE_URL, None) == {}


def test_config_has_no_trial_header() -> None:
    for _stem, version, help_text, era in _GOLDEN_CASES:
        facts = HarnessFacts(version=version, help_text=help_text, era=era)
        for text in config_files(facts, _MODEL, _PROXY_BASE_URL, None).values():
            assert "x-trial-id" not in text


def test_global_model_flag_places_model_before_run() -> None:
    facts = HarnessFacts(version="1.18.25", help_text=_HELP_1X, era="modern-v1")

    command = run_command(facts, _MODEL, _INSTRUCTION, None)

    assert "[ -f ~/.nvm/nvm.sh ] && . ~/.nvm/nvm.sh; " in command
    assert "/usr/local/bin/opencode --model=ahe/mimo-v2.5 run --format=json " in command


def test_run_scoped_model_flag_places_model_after_run() -> None:
    facts = HarnessFacts(version="2.0.32", help_text=_HELP_V2, era="v2")

    command = run_command(facts, _MODEL, _INSTRUCTION, None)

    assert "/usr/local/bin/opencode run --model=ahe/mimo-v2.5 --format=json " in command


@pytest.mark.parametrize(
    ("help_text", "contains", "not_contains"),
    [
        (
            _HELP_MODERN + "-m, --model  model to use\n",
            ["--thinking", "--dangerously-skip-permissions", "--format=json"],
            [],
        ),
        (
            _HELP_LEGACY + "-m, --model  model to use\n",
            ["--thinking", "--format=json"],
            ["--dangerously-skip-permissions"],
        ),
        (
            "--dangerously-skip-permissions  skip permission prompts\n-m, --model  model to use\n",
            ["--dangerously-skip-permissions", "--format=json"],
            ["--thinking"],
        ),
        (
            "-m, --model  model to use\n",
            ["--format=json"],
            ["--thinking", "--dangerously-skip-permissions", "--continue", "--variant"],
        ),
    ],
    ids=["both", "thinking-only", "skip-only", "neither"],
)
def test_run_command_includes_only_advertised_flags(
    help_text: str, contains: list[str], not_contains: list[str]
) -> None:
    facts = HarnessFacts(version="1.18.25", help_text=help_text, era="modern-v1")

    command = run_command(facts, _MODEL, _INSTRUCTION, None)

    for snippet in contains:
        assert snippet in command
    for snippet in not_contains:
        assert snippet not in command


def test_v2_routes_variant_as_model_suffix_and_drops_flag() -> None:
    facts = HarnessFacts(version="2.0.32", help_text=_HELP_V2, era="v2")

    command = run_command(facts, _MODEL, _INSTRUCTION, "high")

    assert "--model=ahe/mimo-v2.5#high" in command
    assert "--variant" not in command


def test_v1_passes_variant_flag_through_untouched() -> None:
    facts = HarnessFacts(version="1.18.25", help_text=_HELP_1X, era="modern-v1")

    command = run_command(facts, _MODEL, _INSTRUCTION, "high")

    assert "--model=ahe/mimo-v2.5 " in command
    assert "#high" not in command
    assert "--variant high " in command


def test_v1_quotes_hostile_variant() -> None:
    facts = HarnessFacts(version="1.18.25", help_text=_HELP_1X, era="modern-v1")
    variant = "high; rm -rf /"

    command = run_command(facts, _MODEL, _INSTRUCTION, variant)

    assert f"--variant {shlex.quote(variant)} " in command
    assert f"--variant {variant} " not in command


def test_run_command_quotes_hostile_instruction() -> None:
    facts = HarnessFacts(version="1.18.25", help_text=_HELP_1X, era="modern-v1")

    command = run_command(facts, "ahe/m", "say 'hi'; rm -rf /", None)

    assert shlex.quote("say 'hi'; rm -rf /") in command


@pytest.mark.parametrize(
    "model",
    [
        "ahe/m; rm -rf /",
        "ahe/m$(whoami)",
        "ahe/m`touch /tmp/pwned`",
        "ahe/m WithSpace",
        "ahe/m'inject",
        'ahe/m"inject',
        "ahe/m|cat /etc/passwd",
        "ahe/m&sleep 1",
        "ahe/m\nrm -rf /",
        "ahe/m>out",
        "-flag/model",
        "mimo-v2.5",
        "",
    ],
    ids=[
        "semicolon",
        "command-substitution",
        "backticks",
        "space",
        "single-quote",
        "double-quote",
        "pipe",
        "ampersand",
        "newline",
        "redirect",
        "leading-dash",
        "no-provider",
        "empty",
    ],
)
def test_run_command_rejects_shell_unsafe_model(model: str) -> None:
    facts = HarnessFacts(version="1.18.25", help_text=_HELP_1X, era="modern-v1")

    with pytest.raises(ValueError, match="shell-safe"):
        run_command(facts, model, _INSTRUCTION, None)


@pytest.mark.parametrize(
    "model",
    [
        "ahe/mimo-v2.5",
        "ahe/m",
        "openai/gpt-4o",
        "ahe/deepseek-v4.1-flash",
        "ahe/qwen2.5:7b",
        "ahe/model@2024+patched",
        "anthropic/claude-3-5-sonnet",
    ],
)
def test_run_command_accepts_safe_model(model: str) -> None:
    facts = HarnessFacts(version="1.18.25", help_text=_HELP_1X, era="modern-v1")

    command = run_command(facts, model, _INSTRUCTION, None)

    assert f"--model={model} " in command


def test_legacy_config_uses_object_permission_and_options_npm() -> None:
    facts = HarnessFacts(version="1.0.19", help_text=_HELP_1X, era="legacy-v1.0")

    config = json.loads(config_files(facts, _MODEL, _PROXY_BASE_URL, None)[_CONFIG_PATH])
    entry = config["provider"]["ahe"]

    assert config["permission"] == {"edit": "allow", "bash": "allow", "webfetch": "allow"}
    assert entry["options"]["npm"] == "@ai-sdk/openai-compatible"


def test_modern_config_uses_string_permission_without_npm() -> None:
    facts = HarnessFacts(version="1.18.25", help_text=_HELP_1X, era="modern-v1")

    config = json.loads(config_files(facts, _MODEL, _PROXY_BASE_URL, None)[_CONFIG_PATH])
    entry = config["provider"]["ahe"]

    assert config["permission"] == "allow"
    assert "npm" not in entry["options"]


def test_v2_config_uses_providers_package_not_npm_or_options() -> None:
    facts = HarnessFacts(version="2.0.32", help_text=_HELP_V2, era="v2")

    config = json.loads(config_files(facts, _MODEL, _PROXY_BASE_URL, None)[_CONFIG_PATH])
    rendered = json.dumps(config)

    assert "providers" in config
    assert "provider" not in config
    assert '"npm"' not in rendered
    assert '"options"' not in rendered
    entry = config["providers"]["ahe"]
    assert entry["package"] == "@opencode/ai/providers/openai-compatible"
    assert entry["settings"] == {"baseURL": _PROXY_BASE_URL, "apiKey": "ahe"}
    assert "headers" not in entry


def test_v1_config_emits_variant_map_and_folds_default() -> None:
    facts = HarnessFacts(version="1.18.25", help_text=_HELP_1X, era="modern-v1")

    config = json.loads(
        config_files(
            facts,
            _MODEL,
            _PROXY_BASE_URL,
            None,
            variants=_VARIANTS_A,
            default_variant="high",
        )[_CONFIG_PATH]
    )
    entry = config["provider"]["ahe"]["models"][_MODEL_ID]

    assert entry["variants"] == _VARIANTS_A
    assert entry["options"]["temperature"] == 0.6
    assert entry["options"]["top_p"] == 0.95
    assert entry["options"]["reasoningEffort"] == "high"


def test_v2_config_emits_variant_array_and_folds_default() -> None:
    facts = HarnessFacts(version="2.0.32", help_text=_HELP_V2, era="v2")

    config = json.loads(
        config_files(
            facts,
            _MODEL,
            _PROXY_BASE_URL,
            None,
            variants=_VARIANTS_B,
            default_variant="deep",
        )[_CONFIG_PATH]
    )
    entry = config["providers"]["ahe"]["models"][_MODEL_ID]

    assert entry["variants"] == [
        {"id": variant_id, "settings": settings} for variant_id, settings in _VARIANTS_B.items()
    ]
    assert entry["settings"]["temperature"] == 0.6
    assert entry["settings"]["top_p"] == 0.95
    assert entry["settings"]["reasoningEffort"] == "max"
    assert entry["settings"]["verbosity"] == "high"


@pytest.mark.parametrize(
    ("selected", "default"),
    [
        ("missing", None),
        (None, "missing"),
    ],
    ids=["selected", "default"],
)
def test_config_rejects_variant_absent_from_supplied_map(
    selected: str | None, default: str | None
) -> None:
    facts = HarnessFacts(version="1.18.25", help_text=_HELP_1X, era="modern-v1")

    with pytest.raises(ValueError, match="variant"):
        config_files(
            facts,
            _MODEL,
            _PROXY_BASE_URL,
            selected,
            variants=_VARIANTS_A,
            default_variant=default,
        )


def test_config_pins_sampling() -> None:
    for _stem, version, help_text, era in _GOLDEN_CASES:
        facts = HarnessFacts(version=version, help_text=help_text, era=era)
        config = json.loads(config_files(facts, _MODEL, _PROXY_BASE_URL, None)[_CONFIG_PATH])
        entry = config.get("providers", config.get("provider"))["ahe"]["models"][_MODEL_ID]
        settings = entry.get("settings", entry.get("options"))
        assert settings["temperature"] == 0.6
        assert settings["top_p"] == 0.95


def test_config_never_emits_legacy_api_key() -> None:
    for _stem, version, help_text, era in _GOLDEN_CASES:
        facts = HarnessFacts(version=version, help_text=help_text, era=era)
        config = json.loads(config_files(facts, _MODEL, _PROXY_BASE_URL, None)[_CONFIG_PATH])
        for entry in config.get("providers", config.get("provider")).values():
            assert "api" not in entry


def test_process_pattern_is_opencode() -> None:
    assert PROCESS_PATTERN == "opencode"

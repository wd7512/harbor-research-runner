import json
from pathlib import Path

import pytest

from harbor_research_runner.job_config.model_cards import ModelCardError, load_card

CARD_ROOT = Path(__file__).parents[3] / "src/harbor_research_runner/job_config/model_cards"


def _write_card(tmp_path: Path, **overrides: object) -> None:
    card: dict[str, object] = {
        "id": "provider/model",
        "upstream_model": "wire",
        "api": "chat",
        "endpoints": ["https://example.test"],
        "key_env": [],
        "tokens_per_minute": None,
        "pricing": {"input": 0, "output": 0, "cached_read": 0},
        "variants": {},
        "withdrawn": False,
        "reason": None,
    }
    card.update(overrides)
    model_dir = tmp_path / "provider"
    model_dir.mkdir(exist_ok=True)
    (model_dir / "model.json").write_text(json.dumps(card), encoding="utf-8")


def test_every_packaged_model_card_loads() -> None:
    for path in CARD_ROOT.glob("**/*.json"):
        model_id = path.relative_to(CARD_ROOT).with_suffix("").as_posix().replace("__colon__", ":")
        assert load_card(model_id).id == model_id


def test_model_id_with_a_colon_loads_from_an_encoded_file_name() -> None:
    assert load_card("ollama/beam:cloud").id == "ollama/beam:cloud"


def test_no_packaged_card_file_name_contains_a_colon() -> None:
    assert all(":" not in path.name for path in CARD_ROOT.glob("**/*.json"))


def test_card_whose_id_differs_from_its_file_name_is_rejected(tmp_path: Path) -> None:
    model_dir = tmp_path / "provider"
    model_dir.mkdir()
    (model_dir / "requested__colon__variant.json").write_text(
        '{"id":"provider/other","upstream_model":"wire","api":"chat",'
        '"endpoints":["https://example.test"],"key_env":[],"tokens_per_minute":null,'
        '"pricing":{"input":0,"output":0,"cached_read":0},"variants":{},'
        '"withdrawn":false,"reason":null}',
        encoding="utf-8",
    )
    with pytest.raises(ModelCardError, match="does not match"):
        load_card("provider/requested:variant", tmp_path)


def test_withdrawn_card_loads_with_withdrawn_true(tmp_path: Path) -> None:
    model_dir = tmp_path / "provider"
    model_dir.mkdir()
    (model_dir / "model.json").write_text(
        '{"id":"provider/model","upstream_model":"wire","api":"chat",'
        '"endpoints":["https://example.test"],"key_env":[],"tokens_per_minute":null,'
        '"pricing":{"input":0,"output":0,"cached_read":0},"variants":{},'
        '"withdrawn":true,"reason":"retired"}',
        encoding="utf-8",
    )
    assert load_card("provider/model", tmp_path).withdrawn is True


def test_every_model_card_has_three_pricing_fields() -> None:
    for path in CARD_ROOT.glob("**/*.json"):
        model_id = path.relative_to(CARD_ROOT).with_suffix("").as_posix().replace("__colon__", ":")
        assert set(load_card(model_id).pricing) == {"input", "output", "cached_read"}


def test_beam_card_loads_with_default_variant_max() -> None:
    assert load_card("ollama/beam:cloud").default_variant == "max"


def test_card_with_no_default_variant_key_loads_with_none(tmp_path: Path) -> None:
    _write_card(tmp_path)
    assert load_card("provider/model", tmp_path).default_variant is None


def test_every_packaged_card_default_variant_is_declared() -> None:
    for path in CARD_ROOT.glob("**/*.json"):
        model_id = path.relative_to(CARD_ROOT).with_suffix("").as_posix().replace("__colon__", ":")
        card = load_card(model_id)
        assert card.default_variant is None or card.default_variant in card.variants
        assert all(card.variants.values())


def test_default_variant_not_in_variants_is_rejected(tmp_path: Path) -> None:
    _write_card(tmp_path, variants={"low": {"reasoningEffort": "low"}}, default_variant="max")
    with pytest.raises(ModelCardError, match="default_variant"):
        load_card("provider/model", tmp_path)


def test_default_variant_with_no_variants_is_rejected(tmp_path: Path) -> None:
    _write_card(tmp_path, default_variant="max")
    with pytest.raises(ModelCardError, match="declares no variants"):
        load_card("provider/model", tmp_path)


def test_empty_variant_id_is_rejected(tmp_path: Path) -> None:
    _write_card(tmp_path, variants={"": {"reasoningEffort": "low"}})
    with pytest.raises(ModelCardError, match="empty variant id"):
        load_card("provider/model", tmp_path)


def test_empty_variant_settings_are_rejected(tmp_path: Path) -> None:
    _write_card(tmp_path, variants={"low": {}})
    with pytest.raises(ModelCardError, match="empty settings"):
        load_card("provider/model", tmp_path)


def test_non_dictionary_variant_settings_are_rejected(tmp_path: Path) -> None:
    _write_card(tmp_path, variants={"low": ["reasoningEffort"]})
    with pytest.raises(ModelCardError, match="must be a dictionary"):
        load_card("provider/model", tmp_path)


def test_non_string_default_variant_is_rejected(tmp_path: Path) -> None:
    _write_card(tmp_path, variants={"low": {"reasoningEffort": "low"}}, default_variant=1)
    with pytest.raises(ModelCardError, match="default_variant must be a string or null"):
        load_card("provider/model", tmp_path)


def test_request_shaping_configuration_loads(tmp_path: Path) -> None:
    _write_card(
        tmp_path,
        request={"drop_message_fields": ["reasoning_content"], "max_tokens_cap": 128},
    )
    card = load_card("provider/model", tmp_path)
    assert card.request == {"drop_message_fields": ["reasoning_content"], "max_tokens_cap": 128}


def test_unknown_request_shaping_key_is_rejected(tmp_path: Path) -> None:
    _write_card(tmp_path, request={"transform": "strip_reasoning"})
    with pytest.raises(ModelCardError, match="unknown request key"):
        load_card("provider/model", tmp_path)


@pytest.mark.parametrize(
    "shaping",
    [
        None,
        {"drop_message_fields": "reasoning_content"},
        {"drop_message_fields": [""]},
        {"drop_message_fields": [1]},
        {"max_tokens_cap": True},
        {"max_tokens_cap": 0},
        {"max_tokens_cap": -1},
        {"max_tokens_cap": 1.5},
    ],
)
def test_invalid_request_shaping_value_is_rejected(tmp_path: Path, shaping: dict) -> None:
    _write_card(tmp_path, request=shaping)
    with pytest.raises(ModelCardError):
        load_card("provider/model", tmp_path)

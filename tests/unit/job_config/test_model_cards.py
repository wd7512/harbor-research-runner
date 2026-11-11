from pathlib import Path

from harbor_research_runner.job_config.model_cards import load_card

CARD_ROOT = Path(__file__).parents[3] / "src/harbor_research_runner/job_config/model_cards"


def test_every_packaged_model_card_loads() -> None:
    for path in CARD_ROOT.glob("**/*.json"):
        model_id = path.relative_to(CARD_ROOT).with_suffix("").as_posix()
        assert load_card(model_id).id == model_id


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
        model_id = path.relative_to(CARD_ROOT).with_suffix("").as_posix()
        assert set(load_card(model_id).pricing) == {"input", "output", "cached_read"}

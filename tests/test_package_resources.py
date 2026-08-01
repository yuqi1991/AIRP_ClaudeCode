from __future__ import annotations

from airp.import_prepare import prepare_card
from airp.resources import packaged_web_root


def test_packaged_import_prepare_accepts_explicit_projection_root(tmp_path) -> None:
    card = tmp_path / "card"
    card.mkdir()
    projection = tmp_path / "projection"

    summary = prepare_card(card, tmp_path, styles_dir=projection)

    assert summary["action"] == "no_card_data"
    assert (projection / "content.js").is_file()
    assert (projection / ".card_path").read_text(encoding="utf-8") == str(card.resolve())


def test_packaged_runtime_resources_are_available() -> None:
    assert (packaged_web_root() / "index.html").is_file()

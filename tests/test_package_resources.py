from __future__ import annotations

from airp.import_prepare import prepare_card
from airp.cli import _ensure_web_assets
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


def test_existing_projection_refreshes_packaged_workspace_ui_without_overwriting_runtime_files(tmp_path) -> None:
    projection = tmp_path / "projection"
    projection.mkdir()
    (projection / "index.html").write_text("<html>old workspace</html>", encoding="utf-8")
    (projection / "studio.html").write_text("<html>old standalone studio</html>", encoding="utf-8")
    (projection / "content.js").write_text("const currentStory = true;", encoding="utf-8")
    (projection / "state.js").write_text("const currentState = true;", encoding="utf-8")

    _ensure_web_assets(projection)

    page = (projection / "index.html").read_text(encoding="utf-8")
    assert 'data-airp-app="game-workspace"' in page
    assert 'id="studio-drawer-host"' in page
    assert not (projection / "studio.html").exists()
    assert (projection / "content.js").read_text(encoding="utf-8") == "const currentStory = true;"
    assert (projection / "state.js").read_text(encoding="utf-8") == "const currentState = true;"

from __future__ import annotations

import importlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from airp.bootstrap import RepositoryLayout, bootstrap_legacy_runtime  # noqa: E402
from airp.launcher import load_legacy_launcher  # noqa: E402
from airp.import_prepare import prepare_card  # noqa: E402
from airp.resources import packaged_web_root  # noqa: E402


def test_repository_layout_points_at_src_and_skills() -> None:
    layout = RepositoryLayout.from_root(ROOT)

    assert layout.root == ROOT.resolve()
    assert layout.src == (ROOT / "src").resolve()
    assert layout.skills == (ROOT / "skills").resolve()


def test_bootstrap_adds_legacy_path_idempotently(tmp_path, monkeypatch) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "legacy_marker.py").write_text("VALUE = 7\n", encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(SRC)])

    layout = bootstrap_legacy_runtime(tmp_path)
    bootstrap_legacy_runtime(tmp_path)

    assert layout.skills == skills.resolve()
    assert sys.path.count(str(skills.resolve())) == 1
    marker = importlib.import_module("legacy_marker")
    assert marker.VALUE == 7


def test_installable_launcher_delegates_to_legacy_module() -> None:
    launcher = load_legacy_launcher(ROOT)

    assert launcher.__name__ == "start_runtime"
    assert launcher.SKILLS == (ROOT / "skills").resolve()
    assert launcher.main.__module__ == "airp.cli"


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

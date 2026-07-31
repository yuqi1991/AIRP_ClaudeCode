from __future__ import annotations

import sys
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "skills"))


def test_legacy_engine_imports_resolve_to_canonical_modules():
    from airp.engine.macros import expand_template as canonical_expand
    from airp.engine.mvu import extract_commands as canonical_extract
    from airp.engine.quality import DefaultQualityGate as CanonicalQualityGate
    from airp.engine.secret_store import LocalSecretStore as CanonicalSecretStore
    from airp.engine.tokens import read_usage_since as canonical_read_usage
    from airp.engine.worldbook import load_worldbook_entry as canonical_load
    from engine.macros import expand_template as legacy_expand
    from engine.mvu import extract_commands as legacy_extract
    from engine.quality import DefaultQualityGate as LegacyQualityGate
    from engine.secret_store import LocalSecretStore as LegacySecretStore
    from engine.tokens import read_usage_since as legacy_read_usage
    from engine.worldbook import load_worldbook_entry as legacy_load

    assert legacy_expand is canonical_expand
    assert legacy_extract is canonical_extract
    assert LegacyQualityGate is CanonicalQualityGate
    assert LegacySecretStore is CanonicalSecretStore
    assert legacy_read_usage is canonical_read_usage
    assert legacy_load is canonical_load


def test_canonical_runtime_imports_without_legacy_skills_path():
    result = subprocess.run(
        [sys.executable, "-c", "import airp.engine.runtime; print('ok')"],
        cwd=ROOT.parent,
        env={"PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "ok"

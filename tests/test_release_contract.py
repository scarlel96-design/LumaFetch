from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_app_parses_and_declares_release_version() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    ast.parse(source)
    assert 'APP_VERSION = "1.14.0"' in source


def test_installer_and_workflow_use_exact_output_name() -> None:
    expected = "LumaFetch-Setup-1.14.0.exe"
    iss = (ROOT / "installer" / "LumaFetch.iss").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "windows-build.yml").read_text(encoding="utf-8")
    assert '#define MyAppVersion "1.14.0"' in iss
    assert "OutputBaseFilename=LumaFetch-Setup-1.14.0" in iss
    assert expected in workflow

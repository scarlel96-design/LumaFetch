"""Design contract: Referer auto/manual controls match existing button styling."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")


def test_referer_mode_uses_ctk_buttons_not_segmented() -> None:
    """App chrome is CTkButton-based; segmented controls would look foreign."""
    assert "CTkSegmentedButton" not in APP_SOURCE
    assert "self.referer_auto_button = ctk.CTkButton(" in APP_SOURCE
    assert "self.referer_manual_button = ctk.CTkButton(" in APP_SOURCE


def test_referer_toggle_palette_matches_secondary_and_accent_buttons() -> None:
    # Secondary buttons used across the form (미리보기 / 즐겨찾기 / 선택).
    assert 'fg_color="#273450"' in APP_SOURCE
    assert 'hover_color="#334364"' in APP_SOURCE
    # Selected state reuses app accent tokens.
    assert 'self.COLORS["accent"]' in APP_SOURCE
    assert 'self.COLORS["accent_hover"]' in APP_SOURCE

    sync = APP_SOURCE.split("def _sync_referer_mode_ui", 1)[1].split("\n    def ", 1)[0]
    assert '"#273450"' in sync
    assert '"#334364"' in sync
    assert 'self.COLORS["accent"]' in sync
    assert 'self.COLORS["accent_hover"]' in sync


def test_referer_toggle_geometry_matches_small_secondary_controls() -> None:
    build = APP_SOURCE.split("def _build_referer_row", 1)[1].split("\n    def ", 1)[0]
    assert 'corner_radius": 11' in build or "corner_radius=11" in build
    assert re.search(r'"height":\s*28|"height": 28|height=28', build)
    assert re.search(r'"width":\s*58|"width": 58|width=58', build)
    # Entry keeps the same field chrome as other form inputs.
    assert 'height=34' in build
    assert 'corner_radius=11' in build
    assert 'fg_color=self.COLORS["input"]' in build
    assert 'border_color="#2A3655"' in build


def test_app_parses_after_referer_ui_changes() -> None:
    ast.parse(APP_SOURCE)

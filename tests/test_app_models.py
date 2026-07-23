from __future__ import annotations

import json
import sys
import types
from pathlib import Path

try:
    import customtkinter  # noqa: F401
except ModuleNotFoundError:
    fake = types.ModuleType("customtkinter")
    fake.CTkFrame = type("CTkFrame", (), {})
    fake.CTk = type("CTk", (), {})
    fake.CTkBaseClass = object
    fake.CTkFont = object
    sys.modules["customtkinter"] = fake

from app import DownloadConfig, FavoritePreset, load_favorites


def make_config(ranges: str) -> DownloadConfig:
    return DownloadConfig(
        template_url="https://example.com/{char}/{situation}.webp",
        character="A",
        ranges=ranges,
        destination=Path.cwd(),
    )


def test_download_config_integrates_mixed_prefixed_ranges() -> None:
    values = make_config("01..50,s01..83").expand_situations()
    assert len(values) == 133
    assert values[0] == "01"
    assert values[50] == "s01"
    assert values[-1] == "s83"


def test_real_favorite_model_skips_only_bad_record(tmp_path: Path) -> None:
    valid = FavoritePreset(
        name="정상",
        template_url="https://example.com/{char}/{situation}.webp",
        character="A",
        ranges="01..02",
    ).model_dump(mode="json")
    path = tmp_path / "favorites.json"
    path.write_text(json.dumps([valid, {"name": "손상"}], ensure_ascii=False), encoding="utf-8")

    favorites, rejected, file_error = load_favorites(path)
    assert [favorite.name for favorite in favorites] == ["정상"]
    assert rejected == 1
    assert file_error is None

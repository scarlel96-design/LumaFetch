"""Favorite cover strip helpers."""

from __future__ import annotations

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

from app import FAVORITE_COVER_LIMIT, FavoritePreset, favorite_cover_fingerprint


def test_favorite_cover_fingerprint_stable_and_sensitive() -> None:
    base = FavoritePreset(
        name="set-a",
        template_url="https://cdn.example.com/{char}/{situation}.webp",
        character="A,B,C,D,E",
        ranges="01..03",
        outfit="X",
    )
    same = FavoritePreset(
        name="renamed",
        template_url="https://cdn.example.com/{char}/{situation}.webp",
        character="A,B,C,D,E",
        ranges="01..03",
        outfit="X",
    )
    different = FavoritePreset(
        name="set-a",
        template_url="https://cdn.example.com/{char}/{situation}.webp",
        character="A,B,C",
        ranges="01..03",
        outfit="X",
    )
    assert favorite_cover_fingerprint(base) == favorite_cover_fingerprint(same)
    assert favorite_cover_fingerprint(base) != favorite_cover_fingerprint(different)
    assert len(favorite_cover_fingerprint(base)) == 20


def test_favorite_cover_limit_constant() -> None:
    assert FAVORITE_COVER_LIMIT == 4

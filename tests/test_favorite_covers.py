"""Favorite cover strip helpers."""

from __future__ import annotations

import sys
import types

try:
    import customtkinter  # noqa: F401
except ModuleNotFoundError:
    fake = types.ModuleType("customtkinter")
    fake.CTkFrame = type("CTkFrame", (), {})
    fake.CTk = type("CTk", (), {})
    fake.CTkBaseClass = object
    fake.CTkFont = object
    sys.modules["customtkinter"] = fake

from app import (
    FAVORITE_COVER_LIMIT,
    FAVORITE_COVER_SITUATION_PROBES,
    FavoritePreset,
    favorite_cover_fingerprint,
    sample_probe_values,
)


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
    assert FAVORITE_COVER_SITUATION_PROBES >= 20


def test_sample_probe_values_keeps_head_and_spreads() -> None:
    values = [f"{index:02d}" for index in range(1, 84)]
    sampled = sample_probe_values(values, 28)
    assert len(sampled) == 28
    assert sampled[0] == "01"
    assert "01" in sampled and "02" in sampled
    # Should reach near the end of a long range, not only the first 8.
    assert any(int(value) >= 70 for value in sampled)
    assert len(set(sampled)) == len(sampled)


def test_sample_probe_values_short_list() -> None:
    assert sample_probe_values(["a", "b"], 8) == ["a", "b"]
    assert sample_probe_values([], 4) == []

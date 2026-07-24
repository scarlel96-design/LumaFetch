"""Multi-outfit expansion for templates that use {outfit} path segments."""

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

from app import DownloadConfig, Downloader
import threading


def test_outfit_list_expands_and_builds_separate_urls() -> None:
    cfg = DownloadConfig(
        template_url="https://example.com/{char}/{outfit}/{situation}.webp",
        character="DNA",
        ranges="1..2",
        outfit="A1,E1,E2,E3,E4",
        destination=Path("."),
    )
    assert cfg.expand_outfits() == ["A1", "E1", "E2", "E3", "E4"]
    assert cfg.job_count() == 1 * 5 * 2
    downloader = Downloader(cfg, threading.Event(), lambda *_args: None)
    assert downloader.make_url("DNA", "1", "A1") == "https://example.com/DNA/A1/1.webp"
    assert downloader.make_url("DNA", "1", "E4") == "https://example.com/DNA/E4/1.webp"
    # Must never glue all outfits into one path segment.
    assert "," not in downloader.make_url("DNA", "1", "E1")


def test_single_outfit_still_defaults_to_x() -> None:
    cfg = DownloadConfig(
        template_url="https://example.com/{char}/{outfit}/{situation}.webp",
        character="DNA",
        ranges="1",
        outfit="",
        destination=Path("."),
    )
    assert cfg.expand_outfits() == ["X"]


def test_templates_without_outfit_do_not_multiply_jobs() -> None:
    cfg = DownloadConfig(
        template_url="https://example.com/{char}/{situation}.webp",
        character="DNA",
        ranges="1..10",
        outfit="A1,E1,E2,E3,E4",
        destination=Path("."),
    )
    assert cfg.uses_outfit_dimension() is False
    assert cfg.active_outfits() == ["A1"]
    assert cfg.job_count() == 10


def test_pose_templates_use_all_outfits() -> None:
    cfg = DownloadConfig(
        template_url="https://example.com/{char}/{pose}.webp",
        character="DNA",
        ranges="1..2",
        outfit="A1,E1",
        destination=Path("."),
    )
    assert cfg.uses_outfit_dimension() is True
    assert cfg.job_count() == 4

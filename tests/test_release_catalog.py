"""Tests for multi-release catalog parsing and version comparison helpers."""

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
    UpdateInfo,
    format_version_tuple,
    parse_github_release,
    parse_release_version,
)


def _asset(version: str, *, digest: str = "sha256:" + ("ab" * 32)) -> dict:
    return {
        "state": "uploaded",
        "name": f"LumaFetch-Setup-{version}.exe",
        "browser_download_url": (
            f"https://github.com/scarlel96-design/LumaFetch/releases/download/v{version}/"
            f"LumaFetch-Setup-{version}.exe"
        ),
        "size": 12_000_000,
        "digest": digest,
    }


def _release(version: str, *, draft: bool = False, prerelease: bool = False) -> dict:
    return {
        "draft": draft,
        "prerelease": prerelease,
        "tag_name": f"v{version}",
        "name": f"Luma Fetch {version}",
        "body": "notes",
        "html_url": f"https://github.com/scarlel96-design/LumaFetch/releases/tag/v{version}",
        "published_at": "2026-07-24T00:00:00Z",
        "assets": [_asset(version)],
    }


def test_parse_github_release_accepts_valid_installer() -> None:
    info = parse_github_release(_release("1.13.1"))
    assert isinstance(info, UpdateInfo)
    assert info.tag_name == "v1.13.1"
    assert info.version == (1, 13, 1)
    assert info.asset_name == "LumaFetch-Setup-1.13.1.exe"
    assert info.asset_sha256 == "ab" * 32
    assert info.is_prerelease is False


def test_parse_github_release_skips_draft_and_bad_digest() -> None:
    assert parse_github_release(_release("1.13.0", draft=True)) is None
    bad = _release("1.12.9")
    bad["assets"][0]["digest"] = "sha256:deadbeef"
    assert parse_github_release(bad) is None


def test_version_compare_supports_downgrade_detection() -> None:
    older = parse_release_version("1.13.0")
    newer = parse_release_version("v1.13.2")
    assert older is not None and newer is not None
    assert older < newer
    assert format_version_tuple(newer) == "1.13.2"

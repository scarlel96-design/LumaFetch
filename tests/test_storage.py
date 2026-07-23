from __future__ import annotations

import json
import os
import time
from pathlib import Path

from lumafetch.storage import (
    atomic_write_json,
    cleanup_stale_part_files,
    load_validated_json_list,
)


def test_one_corrupt_favorite_does_not_hide_valid_entries(tmp_path: Path) -> None:
    path = tmp_path / "favorites.json"
    path.write_text(
        json.dumps([
            {"name": "valid-a"},
            {"broken": True},
            {"name": "valid-b"},
        ]),
        encoding="utf-8",
    )

    def validate(item: object) -> str:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("invalid favorite")
        return item["name"]

    result = load_validated_json_list(
        path,
        validator=validate,
        max_items=100,
        max_bytes=1024 * 1024,
    )
    assert result.items == ["valid-a", "valid-b"]
    assert result.rejected == 1
    assert result.file_error is None


def test_atomic_json_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "favorites.json"
    atomic_write_json(path, [{"name": "one"}])
    assert json.loads(path.read_text(encoding="utf-8")) == [{"name": "one"}]
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


def test_stale_part_cleanup_only_removes_lumafetch_partial_files(tmp_path: Path) -> None:
    old_image_part = tmp_path / ".character_pose.part.webp"
    old_update_part = tmp_path / "LumaFetch-Setup-1.13.0.exe.part"
    unrelated = tmp_path / "notes.partial.txt"
    recent = tmp_path / ".recent.part.png"
    for path in (old_image_part, old_update_part, unrelated, recent):
        path.write_bytes(b"x")
    old = time.time() - 8 * 60 * 60
    os.utime(old_image_part, (old, old))
    os.utime(old_update_part, (old, old))
    os.utime(unrelated, (old, old))

    assert cleanup_stale_part_files(tmp_path, older_than_seconds=6 * 60 * 60) == 2
    assert not old_image_part.exists()
    assert not old_update_part.exists()
    assert unrelated.exists()
    assert recent.exists()

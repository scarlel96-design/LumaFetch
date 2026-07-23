"""Crash-tolerant local persistence and temporary-file housekeeping."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ValidatedListResult(Generic[T]):
    items: list[T]
    rejected: int = 0
    file_error: str | None = None


def load_validated_json_list(
    path: Path,
    *,
    validator: Callable[[Any], T],
    max_items: int,
    max_bytes: int,
) -> ValidatedListResult[T]:
    """Load valid entries independently so one damaged entry cannot hide all others."""
    try:
        if not path.is_file():
            return ValidatedListResult([])
        if path.stat().st_size > max_bytes:
            return ValidatedListResult([], file_error="파일 크기 제한을 초과했습니다.")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return ValidatedListResult([], file_error=str(error))

    if not isinstance(payload, list):
        return ValidatedListResult([], file_error="최상위 JSON 값이 목록이 아닙니다.")

    valid: list[T] = []
    rejected = 0
    for raw in payload[:max_items]:
        try:
            valid.append(validator(raw))
        except Exception:
            rejected += 1
    rejected += max(0, len(payload) - max_items)
    return ValidatedListResult(valid, rejected=rejected)


def atomic_write_json(path: Path, values: Iterable[Any]) -> None:
    """Durably replace a JSON file without exposing a half-written destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(list(values), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def is_lumafetch_part_file(path: Path) -> bool:
    name = path.name.casefold()
    return name.endswith(".part") or (name.startswith(".") and ".part." in name)


def cleanup_stale_part_files(
    root: Path,
    *,
    older_than_seconds: float = 6 * 60 * 60,
    max_scan: int = 20_000,
) -> int:
    """Remove abandoned Luma Fetch partial files without following symlink trees."""
    if not root.is_dir():
        return 0
    now = time.time()
    removed = 0
    scanned = 0
    try:
        candidates = root.rglob("*")
        for path in candidates:
            scanned += 1
            if scanned > max_scan:
                break
            try:
                if path.is_symlink() or not path.is_file() or not is_lumafetch_part_file(path):
                    continue
                if older_than_seconds > 0 and now - path.stat().st_mtime < older_than_seconds:
                    continue
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
    except OSError:
        return removed
    return removed

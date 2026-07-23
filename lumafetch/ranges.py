"""Range expression parsing shared by the GUI, downloader, and tests."""

from __future__ import annotations

import re
from collections.abc import Callable

_RANGE_PATTERN = re.compile(
    r"^\s*(?P<prefix>[^\d\s,]*?)(?P<start>\d+)\s*\.\.\s*"
    r"(?P<end_prefix>[^\d\s,]*?)(?P<end>\d+)\s*$"
)


def expand_code_expression(
    expression: str,
    *,
    validate: Callable[[str], str],
    max_range_items: int,
    max_total_items: int | None = None,
    item_label: str = "코드",
) -> list[str]:
    """Expand comma-separated literals and prefix-aware numeric ranges.

    Supported forms include ``01..50``, ``s01..83``, ``s01..s83`` and any
    mixture of those forms. Order is retained and duplicate values are removed.
    """
    values: dict[str, None] = {}
    for raw_part in expression.split(","):
        part = raw_part.strip()
        if not part:
            raise ValueError(f"빈 {item_label}는 사용할 수 없습니다.")

        match = _RANGE_PATTERN.fullmatch(part)
        if match is None:
            values.setdefault(validate(part), None)
        else:
            prefix = match.group("prefix")
            end_prefix = match.group("end_prefix")
            if end_prefix and end_prefix != prefix:
                raise ValueError(f"범위의 접두사가 서로 다릅니다: {part}")

            start_text = match.group("start")
            end_text = match.group("end")
            start = int(start_text)
            end = int(end_text)
            if start > end:
                raise ValueError(f"잘못된 범위: {part}")
            count = end - start + 1
            if count > max_range_items:
                raise ValueError(f"한 범위는 최대 {max_range_items:,}개까지 가능합니다.")

            width = (
                max(len(start_text), len(end_text))
                if start_text.startswith("0") or end_text.startswith("0")
                else 0
            )
            for number in range(start, end + 1):
                values.setdefault(validate(f"{prefix}{number:0{width}d}"), None)

        if max_total_items is not None and len(values) > max_total_items:
            raise ValueError(f"{item_label}는 최대 {max_total_items:,}개까지 입력할 수 있습니다.")
    return list(values)

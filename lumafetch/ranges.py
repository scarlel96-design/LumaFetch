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

            # Zero-pad only when the user explicitly wrote padded digits (e.g. 01..50, s01..83).
            # A bare "0" in a0..13 must stay a0,a1,...,a13 — not a00,a01.
            # Padding is intentional only for multi-digit tokens that keep a leading zero.
            def _explicit_pad_width(token: str) -> int:
                return len(token) if len(token) > 1 and token.startswith("0") else 0

            width = max(_explicit_pad_width(start_text), _explicit_pad_width(end_text))
            for number in range(start, end + 1):
                token = f"{number:0{width}d}" if width else str(number)
                values.setdefault(validate(f"{prefix}{token}"), None)

        if max_total_items is not None and len(values) > max_total_items:
            raise ValueError(f"{item_label}는 최대 {max_total_items:,}개까지 입력할 수 있습니다.")
    return list(values)

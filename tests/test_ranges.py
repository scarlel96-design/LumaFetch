from __future__ import annotations

import pytest

from lumafetch.ranges import expand_code_expression


def validate(value: str) -> str:
    if not value or any(char in value for char in r'\\/:*?"<>|'):
        raise ValueError("invalid")
    return value


def expand(value: str) -> list[str]:
    return expand_code_expression(
        value,
        validate=validate,
        max_range_items=100_001,
        item_label="상황 코드",
    )


def test_mixed_numeric_and_prefixed_ranges_are_fully_supported() -> None:
    values = expand("01..50,s01..83")
    assert values[:3] == ["01", "02", "03"]
    assert values[49:53] == ["50", "s01", "s02", "s03"]
    assert values[-1] == "s83"
    assert len(values) == 133


def test_explicit_end_prefix_and_literals_keep_order_and_deduplicate() -> None:
    assert expand("s01..s03,02,02,s02") == ["s01", "s02", "s03", "02"]


def test_unpadded_ranges_remain_unpadded() -> None:
    assert expand("1..3,x8..10") == ["1", "2", "3", "x8", "x9", "x10"]


@pytest.mark.parametrize("value", ["s10..x12", "5..1", "1,,2", "a/1"])
def test_invalid_expressions_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        expand(value)

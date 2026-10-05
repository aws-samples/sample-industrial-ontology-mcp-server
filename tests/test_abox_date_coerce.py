"""Tests for xsd:date/dateTime coercion — #20."""
import pytest

from tools.abox_generation import _convert_date, _convert_datetime


class TestConvertDatetime:
    @pytest.mark.parametrize("inp,expected", [
        ("2024-01-15", "2024-01-15T00:00:00"),
        ("2024-01-15T14:30:00", "2024-01-15T14:30:00"),
        ("2024-01-15T14:30:00Z", "2024-01-15T14:30:00"),
        ("2024/01/15", "2024-01-15T00:00:00"),
        ("2024.01.15 10:20:30", "2024-01-15T10:20:30"),
        ("15-01-2024", "2024-01-15T00:00:00"),
        ("15/01/2024", "2024-01-15T00:00:00"),
        ("20240115", "2024-01-15T00:00:00"),
    ])
    def test_valid_formats(self, inp, expected):
        assert _convert_datetime(inp) == expected

    @pytest.mark.parametrize("inp", [
        "", "   ", "invalid", "2024-13-01", "2024-01-32", "abcd-ef-gh",
    ])
    def test_invalid_returns_none(self, inp):
        assert _convert_datetime(inp) is None


class TestConvertDate:
    @pytest.mark.parametrize("inp,expected", [
        ("2024-01-15", "2024-01-15"),
        ("2024/01/15", "2024-01-15"),
        ("15-01-2024", "2024-01-15"),
        ("15/01/2024", "2024-01-15"),
        ("20240115", "2024-01-15"),
    ])
    def test_valid_formats(self, inp, expected):
        assert _convert_date(inp) == expected

    def test_invalid_returns_none(self):
        assert _convert_date("") is None
        assert _convert_date("invalid") is None

"""Tests for the AUTOPOST_HOUR configuration parsing."""

import pytest

from cogs.autopost import _post_hour


class TestPostHour:
    def test_default_is_eight(self, monkeypatch):
        monkeypatch.delenv("AUTOPOST_HOUR", raising=False)
        assert _post_hour() == 8

    @pytest.mark.parametrize("value,expected", [("0", 0), ("14", 14), ("23", 23)])
    def test_valid_hours(self, monkeypatch, value, expected):
        monkeypatch.setenv("AUTOPOST_HOUR", value)
        assert _post_hour() == expected

    @pytest.mark.parametrize("value", ["24", "-1", "noon", "8.5", ""])
    def test_invalid_values_fall_back_to_default(self, monkeypatch, value):
        monkeypatch.setenv("AUTOPOST_HOUR", value)
        assert _post_hour() == 8

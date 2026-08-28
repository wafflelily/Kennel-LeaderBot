"""Tests for /mystats' promotion of "### " lines into embed fields."""

from cogs.stats import split_fields


class TestSplitFields:
    def test_no_headers_gives_one_field(self):
        assert split_fields("Gauntle", ["a", "b"]) == [("Gauntle", "a\nb")]

    def test_headers_become_their_own_fields(self):
        lines = [
            "Days played: **3**",
            "### Hardest answers nobody else got",
            "• A — 5.0% (#700)",
            "• B — 10.0% (#700)",
            "### Easiest answers nobody else got",
            "• C — 95.0% (#700)",
        ]
        assert split_fields("Catfishing", lines) == [
            ("Catfishing", "Days played: **3**"),
            (
                "Hardest answers nobody else got",
                "• A — 5.0% (#700)\n• B — 10.0% (#700)",
            ),
            ("Easiest answers nobody else got", "• C — 95.0% (#700)"),
        ]

    def test_header_with_no_lines_is_dropped(self):
        lines = ["a", "### Empty section"]
        assert split_fields("Game", lines) == [("Game", "a")]

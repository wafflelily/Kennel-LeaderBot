"""Tests for /mystats' promotion of "### " lines into embed fields, and for
fitting those fields within Discord's embed limits."""

from cogs.stats import EMBED_LIMIT, FIELD_LIMIT, fit_fields, split_fields
from leaderboard.base import Trimmable


class TestSplitFields:
    def test_no_headers_gives_one_field(self):
        assert split_fields("Gauntle", ["a", "b"]) == [("Gauntle", ["a", "b"], [])]

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
            ("Catfishing", ["Days played: **3**"], []),
            (
                "Hardest answers nobody else got",
                ["• A — 5.0% (#700)", "• B — 10.0% (#700)"],
                [],
            ),
            ("Easiest answers nobody else got", ["• C — 95.0% (#700)"], []),
        ]

    def test_header_with_no_lines_is_dropped(self):
        lines = ["a", "### Empty section"]
        assert split_fields("Game", lines) == [("Game", ["a"], [])]

    def test_trimmable_lines_are_kept_apart(self):
        lines = ["a", "### Catches", Trimmable(["x", "y"])]
        assert split_fields("Game", lines) == [
            ("Game", ["a"], []),
            ("Catches", [], ["x", "y"]),
        ]


def total(fields: list[tuple[str, str]]) -> int:
    """Characters the fields add to an embed."""
    return sum(len(name) + len(value) for name, value in fields)


def catches(prefix: str, n: int) -> list[str]:
    """``n`` catch lines of about 60 characters each."""
    return [f"• {prefix} question number {i:03} → **some answer** (#{i})" for i in range(n)]


class TestFitFields:
    def test_everything_fits_untouched(self):
        fields = [("Game", ["a"], []), ("Catches", [], ["x", "y"])]
        assert fit_fields(fields) == [("Game", "a"), ("Catches", "x\ny")]

    def test_long_list_is_cut_to_the_field_limit(self):
        fitted = fit_fields([("Catches", [], catches("Q", 100))])
        (_, value), = fitted
        assert len(value) <= FIELD_LIMIT
        shown = value.splitlines()
        assert shown[-1] == f"+{100 - (len(shown) - 1)} more"
        assert shown[:-1] == catches("Q", 100)[: len(shown) - 1]

    def test_short_list_kept_whole_while_long_one_is_cut(self):
        # 100 shrimp catches and 3 too smart ones, in a nearly full embed.
        shrimp, clever = catches("S", 100), catches("C", 3)
        used = EMBED_LIMIT - 1000
        fitted = dict(
            fit_fields(
                [("Shrimp catches", [], shrimp), ("Too smart catches", [], clever)],
                used=used,
            )
        )
        assert fitted["Too smart catches"] == "\n".join(clever)
        assert fitted["Shrimp catches"].splitlines()[-1].startswith("+")
        assert "more" in fitted["Shrimp catches"]
        assert used + total(list(fitted.items())) <= EMBED_LIMIT

    def test_untrimmable_fields_are_never_cut(self):
        big = ["x" * 900]
        fitted = fit_fields(
            [("Other game", big, []), ("Catches", [], catches("Q", 50))],
            used=EMBED_LIMIT - 1200,
        )
        assert fitted[0] == ("Other game", big[0])
        assert fitted[1][1].endswith("more")
        assert EMBED_LIMIT - 1200 + total(fitted) <= EMBED_LIMIT

    def test_ordinary_lines_in_a_trimmable_field_stay(self):
        fitted = fit_fields([("Catches", ["header line"], catches("Q", 100))])
        assert fitted[0][1].startswith("header line\n")

    def test_no_room_at_all_still_says_how_many(self):
        fitted = fit_fields([("Catches", [], catches("Q", 5))], used=EMBED_LIMIT)
        assert fitted == [("Catches", "+5 more")]

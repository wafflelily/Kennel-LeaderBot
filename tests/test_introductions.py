"""Tests for the Introductions cog's intro-message parsing."""

from datetime import date
from types import SimpleNamespace

import pytest

from cogs.introductions import Introductions, _norm

POSTED = date(2026, 3, 10)


@pytest.fixture
def cog():
    return Introductions(SimpleNamespace())


class TestLabelledIntros:
    def test_basic_labelled_intro(self, cog):
        message = (
            "Name - Zoey\n"
            "Pronouns - she/her\n"
            "Location - UK\n"
            "Invited by - gem"
        )
        played_on, payload = cog.parse(message, POSTED)
        assert played_on == POSTED
        assert payload["name"] == "Zoey"
        assert payload["invited_by"] == "gem"
        assert payload["invited_by_id"] is None
        assert payload["labelled"] is True
        assert payload["fields"] == 4

    def test_markdown_wrapped_labels(self, cog):
        message = "**Name:** Kira\n> Age: 26\n**Invited by:** someone cool"
        _, payload = cog.parse(message, POSTED)
        assert payload["name"] == "Kira"
        assert payload["invited_by"] == "someone cool"
        assert payload["labelled"] is True

    def test_separatorless_labels_still_parse(self, cog):
        message = "Name Zoey\nPronouns she/her"
        _, payload = cog.parse(message, POSTED)
        assert payload["name"] == "Zoey"
        assert payload["labelled"] is True

    def test_mentioned_inviter_captured_by_id(self, cog):
        message = "Name - Zoey\nInvited by - <@8008>"
        _, payload = cog.parse(message, POSTED)
        assert payload["invited_by_id"] == 8008
        assert payload["invited_by"] is None

    def test_interests_and_hobbies_normalise_to_one_label(self, cog):
        message = (
            "Name - A\n"
            "Interests / Hobbies - games\n"
            "Hobbies - reading"
        )
        _, payload = cog.parse(message, POSTED)
        # Both lines map to the same "interests" label; first occurrence wins.
        assert payload["fields"] == 2

    def test_repeated_label_counts_once(self, cog):
        message = "Name - A\nName - B\nPronouns - they/them"
        _, payload = cog.parse(message, POSTED)
        assert payload["name"] == "A"
        assert payload["fields"] == 2


class TestFreeformIntros:
    def test_inline_invited_by_with_inferred_name(self, cog):
        message = "Hi! I'm Quinn :3 invited by arty, I'm 29"
        played_on, payload = cog.parse(message, POSTED)
        assert played_on == POSTED
        assert payload["name"] == "Quinn"
        assert payload["invited_by"] == "arty"
        assert payload["labelled"] is False

    def test_inline_invited_by_mention(self, cog):
        message = "hello all, invited by <@99> to hang out here"
        _, payload = cog.parse(message, POSTED)
        assert payload["invited_by_id"] == 99
        assert payload["name"] is None
        assert payload["labelled"] is False

    def test_call_me_name_form(self, cog):
        message = "hey, call me Ash. invited by gem"
        _, payload = cog.parse(message, POSTED)
        assert payload["name"] == "Ash"
        assert payload["invited_by"] == "gem"


class TestNonIntros:
    def test_single_labelled_line_is_not_an_intro(self, cog):
        assert cog.parse("Name - Bob\nhello everyone!!", POSTED) is None

    def test_ordinary_chat(self, cog):
        assert cog.parse("has anyone seen the new movie?", POSTED) is None

    def test_empty_message(self, cog):
        assert cog.parse("", POSTED) is None


class TestNorm:
    def test_casefold_and_whitespace_collapse(self):
        assert _norm("  GeM   Stone ") == "gem stone"

    def test_already_normal(self):
        assert _norm("arty") == "arty"

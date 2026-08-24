"""
Tests for the Introductions resolution pipeline: the inviter precedence chain
(_resolve_invites), forest building, pruning, and image splitting.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from cogs.introductions import Introductions

GUILD_ID = 1


def intro_row(
    author_id,
    *,
    author_name=None,
    name=None,
    invited_by=None,
    invited_by_id=None,
    labelled=True,
    played_on=date(2026, 1, 1),
):
    return {
        "author_id": author_id,
        "author_name": author_name or f"user{author_id}",
        "played_on": played_on,
        "payload": {
            "name": name,
            "invited_by": invited_by,
            "invited_by_id": invited_by_id,
            "fields": 2 if labelled else 0,
            "labelled": labelled,
        },
    }


def member(uid, *, display=None, username=None, global_name=None):
    return SimpleNamespace(
        id=uid,
        display_name=display or f"user{uid}",
        name=username or f"user{uid}",
        global_name=global_name,
    )


def guild_stub(*members):
    return SimpleNamespace(id=GUILD_ID, members=list(members))


@pytest.fixture
def cog(db):
    return Introductions(SimpleNamespace(database=db))


class TestResolveInvites:
    async def test_mention_resolves_directly(self, cog):
        _, edges, unknowns = await cog._resolve_invites(
            guild_stub(), [intro_row(10, invited_by_id=42)]
        )
        assert edges[10] == "id:42"
        assert unknowns == {}

    async def test_self_mention_means_nobody(self, cog):
        _, edges, _ = await cog._resolve_invites(
            guild_stub(), [intro_row(10, invited_by_id=10)]
        )
        assert edges[10] is None

    @pytest.mark.parametrize("text", ["nobody", "N/A", "myself", "-", "no one"])
    async def test_none_tokens_mean_nobody(self, cog, text):
        _, edges, unknowns = await cog._resolve_invites(
            guild_stub(), [intro_row(10, invited_by=text)]
        )
        assert edges[10] is None
        assert unknowns == {}

    async def test_taught_alias_resolves(self, cog, db):
        await db.set_invite_alias(GUILD_ID, "batty", "id:42")
        _, edges, unknowns = await cog._resolve_invites(
            guild_stub(), [intro_row(10, invited_by="Batty")]
        )
        assert edges[10] == "id:42"
        assert unknowns == {}

    async def test_override_beats_the_intro_mention(self, cog, db):
        await db.set_invite_override(GUILD_ID, 10, "id:99")
        _, edges, _ = await cog._resolve_invites(
            guild_stub(), [intro_row(10, invited_by_id=42)]
        )
        assert edges[10] == "id:99"

    async def test_unique_member_name_matches(self, cog):
        guild = guild_stub(member(42, display="Gem"))
        _, edges, _ = await cog._resolve_invites(
            guild, [intro_row(10, invited_by="gem")]
        )
        assert edges[10] == "id:42"

    async def test_declared_intro_name_matches_after_rename(self, cog):
        # 42's intro declared the name "Gem" even though no current member
        # carries that name any more.
        rows = [
            intro_row(42, name="Gem"),
            intro_row(10, invited_by="gem"),
        ]
        _, edges, _ = await cog._resolve_invites(guild_stub(), rows)
        assert edges[10] == "id:42"

    async def test_ambiguous_name_goes_to_unknowns(self, cog):
        guild = guild_stub(member(42, display="Gem"), member(43, username="gem"))
        _, edges, unknowns = await cog._resolve_invites(
            guild, [intro_row(10, invited_by="Gem", author_name="newbie")]
        )
        assert edges[10] is None
        assert unknowns["gem"]["raw"] == "Gem"
        assert unknowns["gem"]["members"] == ["newbie"]

    async def test_unmatched_name_goes_to_unknowns(self, cog):
        _, edges, unknowns = await cog._resolve_invites(
            guild_stub(), [intro_row(10, invited_by="Some Stranger")]
        )
        assert edges[10] is None
        assert unknowns["some stranger"]["raw"] == "Some Stranger"

    async def test_freeform_never_displaces_a_labelled_intro(self, cog):
        rows = [
            intro_row(10, invited_by_id=42, labelled=True, played_on=date(2026, 1, 1)),
            intro_row(10, invited_by="someone else", labelled=False, played_on=date(2026, 2, 1)),
        ]
        intros, edges, _ = await cog._resolve_invites(guild_stub(), rows)
        assert edges[10] == "id:42"
        assert intros[10]["payload"]["labelled"] is True

    async def test_later_labelled_intro_replaces_an_earlier_one(self, cog):
        rows = [
            intro_row(10, invited_by_id=42, labelled=True, played_on=date(2026, 1, 1)),
            intro_row(10, invited_by_id=43, labelled=True, played_on=date(2026, 2, 1)),
        ]
        _, edges, _ = await cog._resolve_invites(guild_stub(), rows)
        assert edges[10] == "id:43"

    async def test_override_covers_members_without_intros(self, cog, db):
        await db.set_invite_override(GUILD_ID, 77, "id:42")
        await db.set_invite_override(GUILD_ID, 78, None)  # explicitly nobody
        _, edges, _ = await cog._resolve_invites(guild_stub(), [])
        assert edges[77] == "id:42"
        assert edges[78] is None


class TestBuildForest:
    def test_chain(self):
        children, roots = Introductions._build_forest(
            {1: None, 2: "id:1", 3: "id:2"}
        )
        assert roots == ["id:1"]
        assert children["id:1"] == ["id:2"]
        assert children["id:2"] == ["id:3"]

    def test_name_target_becomes_a_root(self):
        children, roots = Introductions._build_forest({2: "name:gem"})
        assert roots == ["name:gem"]
        assert children["name:gem"] == ["id:2"]

    def test_cycle_members_are_promoted_to_a_root(self):
        children, roots = Introductions._build_forest({1: "id:2", 2: "id:1"})
        # One member of the cycle becomes a root so both still render.
        assert roots == ["id:1"]
        assert children["id:1"] == ["id:2"]


def presence(present_keys, all_keys):
    return {key: {"present": key in present_keys, "label": key} for key in all_keys}


class TestPrune:
    def test_departed_leaf_is_dropped(self):
        children = {"id:1": ["id:2"]}
        info = presence({"id:1"}, {"id:1", "id:2"})
        keep = {}
        Introductions._prune("id:1", children, info, keep)
        assert keep == {"id:1": True, "id:2": False}

    def test_departed_inviter_with_a_present_descendant_is_kept(self):
        children = {"id:1": ["id:2"], "id:2": ["id:3"]}
        info = presence({"id:3"}, {"id:1", "id:2", "id:3"})
        keep = {}
        Introductions._prune("id:1", children, info, keep)
        assert keep == {"id:1": True, "id:2": True, "id:3": True}


class TestSplitForest:
    @staticmethod
    def _label_info(keys):
        return {key: {"label": key, "present": True} for key in keys}

    def test_small_tree_is_a_single_group(self):
        children = {"id:0": ["id:1", "id:2"]}
        keys = {"id:0", "id:1", "id:2"}
        groups = Introductions._split_forest(
            children, ["id:0"], self._label_info(keys), dict.fromkeys(keys, True)
        )
        assert len(groups) == 1
        assert groups[0]["top"] == "id:0"
        assert groups[0]["parent_label"] is None
        assert [row["stub"] for row in groups[0]["rows"]] == [False, False, False]

    def test_oversized_subtree_splits_into_its_own_image(self):
        # id:1's subtree (itself + 11 children) exceeds SPLIT_THRESHOLD (10).
        big_kids = [f"id:{i}" for i in range(2, 13)]
        children = {"id:0": ["id:1"], "id:1": big_kids}
        keys = {"id:0", "id:1", *big_kids}
        groups = Introductions._split_forest(
            children, ["id:0"], self._label_info(keys), dict.fromkeys(keys, True)
        )
        assert len(groups) == 2

        first, second = groups
        stub = next(row for row in first["rows"] if row["stub"])
        assert stub["key"] == "id:1"
        assert stub["badge"] == 11  # "+11 invited"

        assert second["top"] == "id:1"
        assert second["parent_label"] == "id:0"  # labelled with its origin tree
        assert all(not row["stub"] for row in second["rows"])

        # Every kept person appears exactly once as a full (non-stub) row.
        full = sorted(
            row["key"] for group in groups for row in group["rows"] if not row["stub"]
        )
        assert full == sorted(keys)

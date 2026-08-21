"""
Introductions / invite-map cog for Kennel-LeaderBot.

Parses the server's introduction channel for messages in (roughly) this format:

    Name - Zoey
    Pronouns - She/Her It/Its
    Gender - tgirl
    Invited by - gem
    Age - 23
    Interests/hobbies - arts and crafts, doing weird shit to computers

The format is fuzzy — separators vary (``-``, ``:``, or nothing) and fields can
be missing — but the key field is **Invited by**, which may be a Discord
mention or a written name. From those we build a per-server map of who invited
whom and render it as a self-contained HTML diagram with avatars and names.

Resolution of a written name to a user is automatic when it's unambiguous
(a mention, or a unique match against member names / declared intro names).
When it isn't, the name is remembered as *unknown*; the owner teaches the bot
with ``/whois`` + ``/identify``, and ``/setinviter`` pins corrections per member.

Parsed intros are cached in the database exactly like the leaderboard cogs
(see ``leaderboard.base``): incremental catch-up scans, live capture via
``on_message``, and edit/delete reconciliation. All commands are owner-only
and every piece of state is keyed by server, so servers never see each other's
data.
"""

import asyncio
import html
import io
import re
from collections import defaultdict
from datetime import date, datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context
from PIL import Image, ImageChops, ImageDraw, ImageFont

from leaderboard.base import LeaderboardCog

# Matches one labelled intro line, e.g. "Invited by - gem", "Name: Zoey",
# "Pronouns She/Her", "**Age:** 26". Group 1 is the label, group 2 the value.
# Labels may be wrapped in markdown (bold/italics/headers/bullets) and the
# separator is optional so hyphen-less intros still parse; requiring several
# labelled lines per message (MIN_FIELDS) keeps ordinary chat from matching.
FIELD_RE = re.compile(
    r"^[\s>*_~`#-]*"                                    # bullets/markdown before the label
    r"(name|pronouns|gender|sexuality|location|invited\s*by|age"
    r"|interests(?:\s*/\s*hobbies)?|hobbies)\b"
    r"[\s*_~`]*[-:–—=]*[\s*_~`]*"                       # optional separator + markdown
    r"(.*?)\s*$",
    re.IGNORECASE,
)

# Fallback for freeform intros with no labelled lines, e.g.
# "Hi! I'm Quinn :3 invited by arty, I'm 29". Captures a mention or the words
# after "invited by" up to a natural break (punctuation, emoji colon, mention).
INLINE_INVITED_RE = re.compile(
    r"invited\s+by\s+(<@!?\d+>|[^,.!?\n:;<]+)", re.IGNORECASE
)

# Freeform self-introduction of a name, e.g. "I'm QuantumJump", "call me Ash".
INLINE_NAME_RE = re.compile(
    r"\b(?:i['’]?m|i am|my name is|call me|name['’]?s)\s+([^,.!?\n:;<]{1,40})",
    re.IGNORECASE,
)

# Markdown/decoration characters stripped from the edges of captured values.
MD_STRIP = "*_~` \t"

# A message must have at least this many labelled lines to count as an intro
# (unless it has an inline "invited by", which is intro enough on its own).
MIN_FIELDS = 2

# Matches a Discord user mention, e.g. <@80088516616269824> or <@!...>.
MENTION_RE = re.compile(r"<@!?(\d+)>")

# "Invited by" values that mean "nobody invited me".
NONE_TOKENS = {
    "", "-", "?", "n/a", "na", "none", "nobody", "no one", "noone",
    "myself", "me", "no-one",
}

# Scan introduction channels from the beginning of time (Discord epoch); intros
# aren't monthly, the whole channel history is relevant.
EPOCH = datetime(2015, 1, 1, tzinfo=timezone.utc)

# Colours for the fallback avatar circles of name-only nodes.
FALLBACK_COLOURS = ["#7c5cbf", "#bf5c8f", "#5c8fbf", "#5cbf8f", "#bf8f5c"]

# Shell for the invite-map page. A plain (non-f) string with __TOKEN__
# placeholders so the CSS/JS braces don't need escaping.
_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Invite map — __TITLE__</title>
<style>
  :root { --line:#4e5058; --card:#2b2d31; --border:#3f4147; --muted:#b5bac1; }
  body { background:#1e1f22; color:#dbdee1; margin:24px auto; max-width:860px;
         font-family:'gg sans','Segoe UI',sans-serif; }
  h1 { font-weight:600; margin-bottom:2px; }
  .stats { color:var(--muted); font-size:13px; margin-bottom:14px; }
  #filter { background:var(--card); border:1px solid var(--border); color:#dbdee1;
         border-radius:8px; padding:8px 12px; width:260px; margin-bottom:18px;
         font-size:14px; outline:none; }
  #filter:focus { border-color:#5865f2; }

  /* Vertical indented tree (file-explorer style): breadth grows downward. */
  ul.tree, .tree ul { list-style:none; margin:0; padding-left:34px; }
  ul.tree { padding-left:0; }
  .tree li { position:relative; }
  /* Elbow into each row, and the rail running past it to later siblings. */
  .tree ul li::before { content:''; position:absolute; left:-20px; top:-10px;
         width:16px; height:32px; border-left:2px solid var(--line);
         border-bottom:2px solid var(--line); border-bottom-left-radius:10px; }
  .tree ul li::after { content:''; position:absolute; left:-20px; top:22px;
         bottom:-2px; border-left:2px solid var(--line); }
  .tree ul li:last-child::after { display:none; }

  summary { list-style:none; cursor:pointer; }
  summary::-webkit-details-marker { display:none; }
  summary, .row { display:flex; align-items:center; padding:3px 0; }
  /* Chevron for branches, matching blank space for leaves, so cards align. */
  summary::before { content:'\\25B8'; width:16px; flex:none; text-align:center;
         color:var(--muted); font-size:12px; transition:transform .12s; }
  details[open] > summary::before { transform:rotate(90deg); }
  .row.leaf::before { content:''; width:16px; flex:none; }

  .card { display:inline-flex; align-items:center; gap:8px; background:var(--card);
         border:1px solid var(--border); border-radius:10px; padding:5px 12px 5px 6px;
         white-space:nowrap; }
  summary:hover > .card { border-color:#5865f2; }
  .card.gone { opacity:.55; border-style:dashed; }
  .card.hit { outline:2px solid #f0b232; opacity:1; }
  .avatar { width:30px; height:30px; border-radius:50%; object-fit:cover; flex:none; }
  .avatar.fallback { display:flex; align-items:center; justify-content:center;
         color:#fff; font-weight:700; font-size:14px; }
  .name { font-size:14.5px; }
  .badge { font-size:11px; color:#c7ccd1; background:#404249;
         border-radius:6px; padding:2px 7px; }
  .left-tag { font-size:11px; color:var(--muted); background:#404249;
         border-radius:6px; padding:2px 6px; }
  .unknowns { margin-top:36px; border-top:1px solid var(--border); padding-top:16px; }
  .unknowns h2 { font-size:16px; }
  code { background:var(--card); padding:2px 5px; border-radius:4px; }
</style></head>
<body>
<h1>Invite map — __TITLE__</h1>
<div class="stats">__STATS__</div>
<input id="filter" type="search" placeholder="Filter names…">
<ul class="tree">__TREES__</ul>
__UNKNOWNS__
<script>
const box = document.getElementById('filter');
box.addEventListener('input', () => {
  const q = box.value.trim().toLowerCase();
  if (q) document.querySelectorAll('details').forEach(d => d.open = true);
  document.querySelectorAll('.card').forEach(c =>
    c.classList.toggle('hit', !!q && c.dataset.name.includes(q)));
});
</script>
</body></html>
"""


def _norm(text: str) -> str:
    """Normalize a name for matching: casefold, strip, collapse whitespace."""
    return re.sub(r"\s+", " ", text.strip()).casefold()


class Introductions(LeaderboardCog, name="introductions"):
    GAME = "introductions"

    def __init__(self, bot) -> None:
        super().__init__(bot)
        # Avatar bytes cached by URL so repeat /invitemap runs don't re-download.
        self._avatar_cache: dict[str, bytes | None] = {}

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #

    def parse(self, content: str, posted_on: date):
        """
        Parse an introduction message into ``(posted_on, payload)`` for the cache.

        Two shapes are recognised:
          * labelled intros — at least MIN_FIELDS labelled lines (markdown around
            labels is tolerated, e.g. ``**Invited by:** Kira``);
          * freeform intros — no labelled lines, but an inline "invited by X"
            phrase (e.g. "Hi! I'm Quinn :3 invited by arty, I'm 29").

        ``payload`` holds the declared ``name`` (labelled, or inferred from
        "I'm X"), the raw ``invited_by`` text (mentions stripped), the
        ``invited_by_id`` when the inviter was pinged, the labelled-field count,
        and ``labelled`` — used to stop a later freeform message from displacing
        a proper intro by the same author. Returns None for ordinary chat.
        """
        fields: dict[str, str] = {}
        for line in content.splitlines():
            match = FIELD_RE.match(line)
            if match is None:
                continue
            label = re.sub(r"\s+", " ", match.group(1).casefold())
            if label.startswith("interests") or label == "hobbies":
                label = "interests"
            if label.startswith("invited"):
                label = "invited by"
            # First occurrence wins if a label somehow repeats.
            fields.setdefault(label, match.group(2).strip(MD_STRIP))

        labelled = len(fields) >= MIN_FIELDS

        # "Invited by" from a labelled line, else from an inline phrase.
        invited_raw = fields.get("invited by")
        if invited_raw is None:
            inline = INLINE_INVITED_RE.search(content)
            if inline:
                invited_raw = inline.group(1)

        # Not enough labelled lines and no inline invited-by: ordinary chat.
        if not labelled and invited_raw is None:
            return None

        invited_by_id = None
        invited_by = None
        if invited_raw is not None:
            mention = MENTION_RE.search(invited_raw)
            if mention:
                invited_by_id = int(mention.group(1))
            # Keep the text with mentions removed as the fallback/name form.
            text = MENTION_RE.sub("", invited_raw).strip(" \t,;&+" + MD_STRIP)
            invited_by = text or None

        # Declared name: labelled field, else a freeform "I'm X" / "call me X".
        name = fields.get("name")
        if not name:
            inline_name = INLINE_NAME_RE.search(content)
            if inline_name:
                name = inline_name.group(1).strip(MD_STRIP)

        return posted_on, {
            "name": name or None,
            "invited_by": invited_by,
            "invited_by_id": invited_by_id,
            "fields": len(fields),
            "labelled": labelled,
        }

    # ------------------------------------------------------------------ #
    # Resolution: raw "invited by" -> node key ('id:<uid>' or 'name:<key>')
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_name_index(guild: discord.Guild | None, intros: dict[int, dict]):
        """
        Map normalized names to the user ids they could refer to.

        Draws on current member names (nick, username, global name) and on what
        each intro author declared as their own name — so "invited by gem" can
        match the person whose intro says "Name - Gem" even after a rename.
        """
        index: dict[str, set[int]] = defaultdict(set)
        for author_id, row in intros.items():
            declared = row["payload"].get("name")
            if declared:
                index[_norm(declared)].add(author_id)
            index[_norm(row["author_name"])].add(author_id)
        if guild is not None:
            for member in guild.members:
                for name in {member.display_name, member.name, member.global_name}:
                    if name:
                        index[_norm(name)].add(member.id)
        return index

    async def _resolve_invites(self, guild: discord.Guild, rows: list[dict]):
        """
        Turn cached intro rows into the invite mapping for a server.

        Returns ``(intros, edges, unknowns)``:
          * ``intros``  - latest intro row per author id.
          * ``edges``   - author id -> inviter node key ('id:...'/'name:...') or
                          None for "nobody / not stated".
          * ``unknowns``- normalized unresolved text -> {"raw", "members"} — the
                          names the bot couldn't attribute; fed to /whois.
        """
        # Latest intro per author (rows arrive ordered by date ascending) —
        # except that a freeform message never displaces a proper labelled
        # intro, so casual chat containing "invited by ..." can't overwrite
        # someone's real introduction. (Old cached rows predate the "labelled"
        # flag; treat them as labelled.)
        intros: dict[int, dict] = {}
        for row in rows:
            prev = intros.get(row["author_id"])
            if (
                prev is not None
                and prev["payload"].get("labelled", True)
                and not row["payload"].get("labelled", True)
            ):
                continue
            intros[row["author_id"]] = row

        aliases = await self.bot.database.get_invite_aliases(guild.id)
        overrides = await self.bot.database.get_invite_overrides(guild.id)
        index = self._build_name_index(guild, intros)

        edges: dict[int, str | None] = {}
        unknowns: dict[str, dict] = {}
        for author_id, row in intros.items():
            # 1. A manual override always wins.
            if author_id in overrides:
                edges[author_id] = overrides[author_id]
                continue
            payload = row["payload"]
            # 2. A mention is unambiguous.
            if payload.get("invited_by_id"):
                target_id = payload["invited_by_id"]
                edges[author_id] = None if target_id == author_id else f"id:{target_id}"
                continue
            raw = payload.get("invited_by")
            if raw is None or _norm(raw) in NONE_TOKENS:
                edges[author_id] = None
                continue
            key = _norm(raw)
            # 3. A previously taught alias.
            if key in aliases:
                edges[author_id] = aliases[key]
                continue
            # 4. A unique match against known names is "obvious" — take it.
            candidates = index.get(key, set()) - {author_id}
            if len(candidates) == 1:
                edges[author_id] = f"id:{next(iter(candidates))}"
                continue
            # 5. Zero or several candidates: remember that we don't know.
            edges[author_id] = None
            entry = unknowns.setdefault(key, {"raw": raw, "members": []})
            entry["members"].append(row["author_name"])

        # Overrides can also cover members with no (parsed) intro at all —
        # including people who only appear on the map as an inviter. Give them
        # an edge too, so /setinviter works for everyone, not just intro
        # authors.
        for member_id, target in overrides.items():
            if member_id not in edges:
                edges[member_id] = (
                    None if target == f"id:{member_id}" else target
                )

        return intros, edges, unknowns

    # ------------------------------------------------------------------ #
    # Tree building and rendering
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_forest(edges: dict[int, str | None]):
        """
        Build the invite forest from the resolved edges.

        Returns ``(children, roots)`` over node keys. Roots are nodes with no
        (kept) inviter; members trapped in an invite cycle are promoted to
        roots so they still render.
        """
        children: dict[str, list[str]] = defaultdict(list)
        parent: dict[str, str] = {}
        nodes: set[str] = set()
        for author_id, target in edges.items():
            member_key = f"id:{author_id}"
            nodes.add(member_key)
            if target is not None:
                nodes.add(target)
                parent[member_key] = target
                children[target].append(member_key)

        roots = sorted(n for n in nodes if n not in parent)

        # Anything not reachable from a root is part of a cycle (a invited b,
        # b invited a). Promote one member of each cycle to a root.
        reachable: set[str] = set()

        def visit(node: str) -> None:
            if node in reachable:
                return
            reachable.add(node)
            for child in children.get(node, ()):
                visit(child)

        for root in roots:
            visit(root)
        for node in sorted(nodes - reachable):
            if node not in reachable:
                roots.append(node)
                visit(node)

        return children, roots

    async def _node_info(
        self,
        guild: discord.Guild,
        keys: set[str],
        intros: dict[int, dict],
    ) -> dict[str, dict]:
        """
        Gather display info for every node that will be rendered.

        Returns node key -> {"label", "avatar", "present"}. Current members get
        their live nickname and avatar; departed users are looked up via the API
        for their last known name/avatar (falling back to the stored intro
        name); 'name:' nodes get a lettered placeholder.
        """
        info: dict[str, dict] = {}
        for key in keys:
            if key.startswith("name:"):
                info[key] = {"label": key[5:], "avatar": None, "present": False}
                continue
            uid = int(key[3:])
            member = guild.get_member(uid)
            if member is not None:
                info[key] = {
                    "label": member.display_name,
                    "avatar": member.display_avatar.url,
                    "present": True,
                }
                continue
            # Departed user: try the API for their last global name/avatar.
            label, avatar = None, None
            try:
                user = await self.bot.fetch_user(uid)
                label = user.global_name or user.name
                avatar = user.display_avatar.url
            except discord.HTTPException:
                pass
            if label is None:
                row = intros.get(uid)
                label = row["author_name"] if row else f"User {uid}"
            info[key] = {"label": label, "avatar": avatar, "present": False}
        return info

    @classmethod
    def _prune(cls, node: str, children, info, keep: dict[str, bool]) -> bool:
        """
        Decide which nodes to show: a current member is always shown; a departed
        user (or a name-only node) is shown only if someone in their invite
        subtree is still in the server.
        """
        any_child = False
        for child in children.get(node, ()):
            any_child = cls._prune(child, children, info, keep) or any_child
        keep[node] = info[node]["present"] or any_child
        return keep[node]

    @staticmethod
    def _render_html(guild_name: str, children, roots, info, keep, unknowns) -> str:
        """
        Render the invite forest as a self-contained HTML document.

        Layout is a *vertical* indented tree (like a file explorer): invite
        trees are wide and shallow, so growing downward keeps everything on
        screen and scrolling natural, where a top-down chart sprawled off the
        sides. Branches are collapsible (<details>), children are sorted with
        the biggest invite subtrees first, each inviter shows how many people
        they brought in, and a filter box highlights matching names.
        """

        # Kept-subtree size per node (self + shown descendants), for sorting
        # and the "invited N" badges.
        sizes: dict[str, int] = {}

        def size(key: str) -> int:
            if key in sizes:
                return sizes[key]
            total = 1 if keep.get(key) else 0
            for child in children.get(key, ()):
                total += size(child)
            sizes[key] = total
            return total

        def node_html(key: str) -> str:
            if not keep.get(key):
                return ""
            node = info[key]
            label = html.escape(node["label"])
            if node["avatar"]:
                avatar = f'<img class="avatar" src="{html.escape(node["avatar"])}" alt="" loading="lazy">'
            else:
                colour = FALLBACK_COLOURS[sum(map(ord, key)) % len(FALLBACK_COLOURS)]
                initial = html.escape(node["label"][:1].upper() or "?")
                avatar = (
                    f'<div class="avatar fallback" '
                    f'style="background:{colour}">{initial}</div>'
                )
            left = "" if node["present"] else '<span class="left-tag">left</span>'

            # Biggest invite branches first, then alphabetical.
            kids = sorted(
                (c for c in children.get(key, ()) if keep.get(c)),
                key=lambda c: (-size(c), info[c]["label"].casefold()),
            )
            descendants = size(key) - 1
            badge = (
                f'<span class="badge">invited {descendants}</span>'
                if descendants
                else ""
            )
            card = (
                f'<div class="card{"" if node["present"] else " gone"}" '
                f'data-name="{html.escape(node["label"].casefold())}">'
                f'{avatar}<span class="name">{label}</span>{left}{badge}</div>'
            )
            if kids:
                kids_html = "".join(node_html(c) for c in kids)
                return (
                    f'<li><details open><summary>{card}</summary>'
                    f"<ul>{kids_html}</ul></details></li>"
                )
            return f'<li><div class="row leaf">{card}</div></li>'

        roots_sorted = sorted(
            (r for r in roots if keep.get(r)),
            key=lambda r: (-size(r), info[r]["label"].casefold()),
        )
        trees = "".join(node_html(root) for root in roots_sorted)

        shown = sum(1 for k, v in keep.items() if v)
        present = sum(1 for k, v in keep.items() if v and info[k]["present"])
        stats = (
            f"{shown} people &bull; {present} still here &bull; "
            f"{shown - present} shown but gone &bull; {len(roots_sorted)} roots"
        )

        unknown_html = ""
        if unknowns:
            items = "".join(
                f"<li><b>{html.escape(entry['raw'])}</b> — from "
                f"{html.escape(', '.join(entry['members']))}</li>"
                for entry in unknowns.values()
            )
            unknown_html = (
                '<div class="unknowns"><h2>Unresolved "invited by" names</h2>'
                f"<p>Teach these with <code>/identify</code>.</p><ul>{items}</ul></div>"
            )

        page = _HTML_TEMPLATE
        for token, value in (
            ("__TITLE__", html.escape(guild_name)),
            ("__STATS__", stats),
            ("__TREES__", trees),
            ("__UNKNOWNS__", unknown_html),
        ):
            page = page.replace(token, value)
        return page

    # ------------------------------------------------------------------ #
    # PNG rendering (shown inline in the Discord embed)
    # ------------------------------------------------------------------ #

    #: A person whose (kept) invite subtree has more than this many people is
    #: split off into their own image; their row in the parent image becomes a
    #: stub with a "+ X invited" label so context isn't lost.
    SPLIT_THRESHOLD = 10

    @classmethod
    def _split_forest(cls, children, roots, info, keep) -> list[dict]:
        """
        Flatten the kept forest into draw-order row groups, one group per image.

        Each root starts a group. Inside a group, any node (other than the
        group's own top) whose kept subtree exceeds SPLIT_THRESHOLD is emitted
        as a *stub* row — drawn with a "+ X invited" label — and queued to
        become the top of its own group, recursively. Every kept person
        therefore appears exactly once as a full row somewhere; stubs are the
        only (intentional) duplicates.

        Returns a list of ``{"top", "parent_label", "rows"}`` where each row is
        ``{"key", "depth", "parent" (row index), "badge", "stub"}``. Groups for
        a root's branches immediately follow that root's group.
        """
        sizes: dict[str, int] = {}

        def size(key: str) -> int:
            if key in sizes:
                return sizes[key]
            total = 1 if keep.get(key) else 0
            for child in children.get(key, ()):
                total += size(child)
            sizes[key] = total
            return total

        def sort_key(key):
            return (-size(key), info[key]["label"].casefold())

        groups: list[dict] = []

        for root in sorted((r for r in roots if keep.get(r)), key=sort_key):
            # (top key, label of the tree it was split out of)
            queue: list[tuple[str, str | None]] = [(root, None)]
            while queue:
                top, parent_label = queue.pop(0)
                rows: list[dict] = []

                def walk(key: str, depth: int, parent: int | None) -> None:
                    if not keep.get(key):
                        return
                    index = len(rows)
                    descendants = size(key) - 1
                    # Big subtrees (other than this image's own top) split off.
                    if index > 0 and descendants > cls.SPLIT_THRESHOLD:
                        rows.append(
                            {
                                "key": key,
                                "depth": depth,
                                "parent": parent,
                                "badge": descendants,
                                "stub": True,
                            }
                        )
                        queue.append((key, info[top]["label"]))
                        return
                    rows.append(
                        {
                            "key": key,
                            "depth": depth,
                            "parent": parent,
                            "badge": descendants,
                            "stub": False,
                        }
                    )
                    for child in sorted(
                        (c for c in children.get(key, ()) if keep.get(c)),
                        key=sort_key,
                    ):
                        walk(child, depth + 1, index)

                walk(top, 0, None)
                groups.append(
                    {"top": top, "parent_label": parent_label, "rows": rows}
                )

        return groups

    async def _fetch_avatars(self, keys, info) -> dict[str, bytes | None]:
        """Download (and cache) the avatar image for every row being drawn."""

        async def fetch(session: aiohttp.ClientSession, url: str) -> bytes | None:
            if url in self._avatar_cache:
                return self._avatar_cache[url]
            data = None
            try:
                # Ask the CDN for a small size; we draw at 32px.
                small = url.split("?")[0] + "?size=64"
                async with session.get(
                    small, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.read()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            self._avatar_cache[url] = data
            return data

        urls = {info[key]["avatar"] for key in keys if info[key]["avatar"]}
        async with aiohttp.ClientSession() as session:
            fetched = await asyncio.gather(*(fetch(session, url) for url in urls))
        by_url = dict(zip(urls, fetched))
        return {
            key: by_url.get(info[key]["avatar"]) if info[key]["avatar"] else None
            for key in keys
        }

    @staticmethod
    def _load_font(size: int):
        """Best-available UI font: Segoe (Windows) / DejaVu (Linux) / default."""
        for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default(size)

    @classmethod
    def _render_png(cls, title, rows, info, avatars) -> bytes:
        """
        Draw one invite tree as a PNG: one row per person, indented under
        their inviter with connector lines — the same vertical layout as the
        HTML, so the images and the interactive file always agree.
        """
        # Layout constants (Discord dark theme).
        pad, row_h, indent, av = 24, 44, 34, 32
        bg, line = (30, 31, 34), (78, 80, 88)
        fg, muted = (219, 222, 225), (148, 155, 164)
        accent = (110, 130, 255)  # stub "+ X invited" labels
        font = cls._load_font(16)
        font_small = cls._load_font(12)
        font_title = cls._load_font(22)

        header_h = 74
        measurer = ImageDraw.Draw(Image.new("RGB", (1, 1)))

        def row_text(row):
            node = info[row["key"]]
            if row.get("stub"):
                # This person's subtree continues in their own image.
                badge = f"  ·  + {row['badge']} invited"
            elif row["badge"]:
                badge = f"  ·  invited {row['badge']}"
            else:
                badge = ""
            left = "  (left)" if not node["present"] else ""
            return node["label"], badge + left

        width = pad * 2 + int(
            max(
                measurer.textlength(title, font_title),
                *(
                    row["depth"] * indent
                    + av
                    + 10
                    + measurer.textlength(row_text(row)[0], font)
                    + measurer.textlength(row_text(row)[1], font_small)
                    for row in rows
                ),
            )
        )
        width = max(480, min(width, 1600))
        height = header_h + pad + len(rows) * row_h + pad

        image = Image.new("RGB", (width, height), bg)
        draw = ImageDraw.Draw(image)
        draw.text((pad, pad), title, font=font_title, fill=fg)

        def center_y(index: int) -> int:
            return header_h + pad + index * row_h + row_h // 2

        # Connector lines first, so the avatars draw over the joints.
        last_child_of: dict[int, int] = {}
        for i, row in enumerate(rows):
            if row["parent"] is not None:
                last_child_of[row["parent"]] = i
        for i, row in enumerate(rows):
            if row["parent"] is None:
                continue
            parent = rows[row["parent"]]
            px = pad + parent["depth"] * indent + av // 2
            cy = center_y(i)
            draw.line(
                [(px, cy), (pad + row["depth"] * indent - 4, cy)], fill=line, width=2
            )
        for parent_index, last_index in last_child_of.items():
            parent = rows[parent_index]
            px = pad + parent["depth"] * indent + av // 2
            draw.line(
                [(px, center_y(parent_index) + av // 2 + 2), (px, center_y(last_index))],
                fill=line,
                width=2,
            )

        # Circular avatar mask, reused for every row.
        mask = Image.new("L", (av, av), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, av - 1, av - 1), fill=255)

        for i, row in enumerate(rows):
            node = info[row["key"]]
            x = pad + row["depth"] * indent
            cy = center_y(i)
            top = cy - av // 2

            data = avatars.get(row["key"])
            pasted = False
            if data:
                try:
                    avatar = (
                        Image.open(io.BytesIO(data))
                        .convert("RGBA")
                        .resize((av, av))
                    )
                    if not node["present"]:  # fade people who left
                        avatar.putalpha(
                            avatar.getchannel("A").point(lambda a: a * 55 // 100)
                        )
                    # Clip to a circle *and* respect the (possibly faded) alpha.
                    paste_mask = ImageChops.multiply(mask, avatar.getchannel("A"))
                    image.paste(avatar, (x, top), paste_mask)
                    pasted = True
                except OSError:
                    pass
            if not pasted:
                colour = FALLBACK_COLOURS[
                    sum(map(ord, row["key"])) % len(FALLBACK_COLOURS)
                ]
                rgb = tuple(int(colour[j : j + 2], 16) for j in (1, 3, 5))
                draw.ellipse((x, top, x + av - 1, top + av - 1), fill=rgb)
                initial = (node["label"][:1] or "?").upper()
                draw.text(
                    (x + av // 2, cy), initial, font=font, fill=(255, 255, 255),
                    anchor="mm",
                )

            name_x = x + av + 10
            name, extra = row_text(row)
            draw.text(
                (name_x, cy), name, font=font,
                fill=fg if node["present"] else muted, anchor="lm",
            )
            if extra:
                draw.text(
                    (name_x + measurer.textlength(name, font) + 2, cy),
                    extra, font=font_small,
                    fill=accent if row.get("stub") else muted, anchor="lm",
                )

        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()

    # ------------------------------------------------------------------ #
    # Shared helpers for the commands
    # ------------------------------------------------------------------ #

    async def _get_intro_channel(self, context: Context):
        """Return this server's configured intro channel, or None (with error sent)."""
        channel_id = await self.bot.database.get_intro_channel(context.guild.id)
        if channel_id is None:
            await context.send(
                embed=discord.Embed(
                    title="Error!",
                    description=(
                        "No introduction channel is set for this server.\n"
                        "Set one with `/introchannel #channel` first."
                    ),
                    color=0xE02B2B,
                )
            )
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None or channel.guild.id != context.guild.id:
            await context.send(
                embed=discord.Embed(
                    title="Error!",
                    description=(
                        "The configured introduction channel no longer exists in "
                        "this server. Set a new one with `/introchannel #channel`."
                    ),
                    color=0xE02B2B,
                )
            )
            return None
        return channel

    @staticmethod
    def _parse_target(target: str) -> str:
        """Turn an /identify or /setinviter target into a node key."""
        mention = MENTION_RE.search(target)
        if mention:
            return f"id:{mention.group(1)}"
        text = target.strip()
        if text.isdigit():
            return f"id:{text}"
        return f"name:{_norm(text)}"

    async def _load_unknowns(self, context: Context):
        """Sync the intro channel and return the current unknown-name map."""
        channel = await self._get_intro_channel(context)
        if channel is None:
            return None
        await self._sync_channel(channel, EPOCH)
        rows = await self._load_all(channel.id)
        _, _, unknowns = await self._resolve_invites(context.guild, rows)
        return unknowns

    # ------------------------------------------------------------------ #
    # Commands (owner only, guild only)
    # ------------------------------------------------------------------ #

    @commands.hybrid_command(
        name="introchannel",
        description="Set which channel holds this server's introductions.",
    )
    @app_commands.describe(channel="The channel introduction messages are posted in.")
    @commands.is_owner()
    @commands.guild_only()
    async def introchannel(self, context: Context, channel: discord.TextChannel) -> None:
        """
        Set the introduction channel for this server.

        :param context: The hybrid command context.
        :param channel: The channel introduction messages are posted in.
        """
        await self.bot.database.set_intro_channel(context.guild.id, channel.id)
        await context.send(
            embed=discord.Embed(
                description=f"Introduction channel set to {channel.mention}.",
                color=0xBEBEFE,
            )
        )

    @commands.hybrid_command(
        name="invitemap",
        description="Build the invite map from the introduction channel.",
    )
    @commands.is_owner()
    @commands.guild_only()
    async def invitemap(self, context: Context) -> None:
        """
        Scan the introduction channel and post the invite map as an HTML diagram.

        :param context: The hybrid command context.
        """
        channel = await self._get_intro_channel(context)
        if channel is None:
            return

        await context.defer()
        try:
            await self._sync_channel(channel, EPOCH)
        except discord.Forbidden:
            await context.send(
                embed=discord.Embed(
                    title="Error!",
                    description=(
                        f"I can't read the history of {channel.mention}. Please give "
                        "me **View Channel** and **Read Message History** there."
                    ),
                    color=0xE02B2B,
                )
            )
            return

        rows = await self._load_all(channel.id)
        if not rows:
            await context.send(
                embed=discord.Embed(
                    title="🗺️ Invite map",
                    description=f"No introductions found in {channel.mention}.",
                    color=0xE02B2B,
                )
            )
            return

        intros, edges, unknowns = await self._resolve_invites(context.guild, rows)
        children, roots = self._build_forest(edges)

        all_keys = set(children) | {c for kids in children.values() for c in kids}
        all_keys |= set(roots)
        info = await self._node_info(context.guild, all_keys, intros)

        keep: dict[str, bool] = {}
        for root in roots:
            self._prune(root, children, info, keep)

        page = self._render_html(
            context.guild.name, children, roots, info, keep, unknowns
        )

        # One PNG per group: each root is a group, and any subtree bigger than
        # SPLIT_THRESHOLD is split into its own follow-on image (shown as a
        # "+ X invited" stub in its parent's image). Childless roots are
        # combined into one "no recorded invitees" image.
        groups = self._split_forest(children, roots, info, keep)
        tree_groups = [g for g in groups if len(g["rows"]) > 1]
        singles = [g["rows"][0] for g in groups if len(g["rows"]) == 1]

        all_keys = [row["key"] for g in tree_groups for row in g["rows"]]
        all_keys += [row["key"] for row in singles]
        avatars = await self._fetch_avatars(all_keys, info)

        files: list[discord.File] = []
        for i, group in enumerate(tree_groups, 1):
            top_label = info[group["top"]]["label"]
            if group["parent_label"] is None:
                title = f"{top_label}'s tree"
            else:
                title = f"{top_label}'s branch (from {group['parent_label']}'s tree)"
            png = self._render_png(title, group["rows"], info, avatars)
            safe = re.sub(r"[^A-Za-z0-9_-]+", "_", top_label)[:32] or "tree"
            files.append(
                discord.File(io.BytesIO(png), filename=f"invitemap-{i}-{safe}.png")
            )
        if singles:
            singles.sort(key=lambda row: info[row["key"]]["label"].casefold())
            png = self._render_png("No recorded invitees", singles, info, avatars)
            files.append(
                discord.File(io.BytesIO(png), filename="invitemap-unconnected.png")
            )

        shown = sum(1 for v in keep.values() if v)

        summary = discord.Embed(
            title="🗺️ Invite map",
            description=(
                f"**{len(intros)}** introductions parsed • **{shown}** people shown "
                f"• **{len(files)}** image{'s' if len(files) != 1 else ''}"
            ),
            color=0xBEBEFE,
        )
        if unknowns:
            listed = "\n".join(
                f"• `{entry['raw']}` (from {', '.join(entry['members'])})"
                for entry in list(unknowns.values())[:10]
            )
            more = "" if len(unknowns) <= 10 else f"\n…and {len(unknowns) - 10} more."
            summary.add_field(
                name=f"❓ {len(unknowns)} unresolved name{'s' if len(unknowns) != 1 else ''}",
                value=f"{listed}{more}\nUse `/whois` and `/identify` to teach me.",
                inline=False,
            )

        # Summary + interactive HTML first, then the tree images in batches of
        # up to 10 (Discord's attachment limit per message).
        await context.send(
            embed=summary,
            file=discord.File(
                io.BytesIO(page.encode("utf-8")), filename="invitemap.html"
            ),
        )
        for start in range(0, len(files), 10):
            await context.send(files=files[start : start + 10])

    @commands.hybrid_command(
        name="whois",
        description="List the 'invited by' names the bot couldn't match to a user.",
    )
    @commands.is_owner()
    @commands.guild_only()
    async def whois(self, context: Context) -> None:
        """
        List unresolved "invited by" names so the owner can /identify them.

        :param context: The hybrid command context.
        """
        await context.defer()
        unknowns = await self._load_unknowns(context)
        if unknowns is None:
            return
        if not unknowns:
            await context.send(
                embed=discord.Embed(
                    description="No unresolved names — everyone is attributed. 🎉",
                    color=0xBEBEFE,
                )
            )
            return
        lines = [
            f"• Who is **{entry['raw']}**? (named by {', '.join(entry['members'])})"
            for entry in unknowns.values()
        ]
        await context.send(
            embed=discord.Embed(
                title="❓ Unresolved inviters",
                description=(
                    "\n".join(lines)
                    + "\n\nAnswer with `/identify <name> <@user or a unique name>`."
                ),
                color=0xBEBEFE,
            )
        )

    @commands.hybrid_command(
        name="uninvited",
        description="List server members with no inviter recorded.",
    )
    @commands.is_owner()
    @commands.guild_only()
    async def uninvited(self, context: Context) -> None:
        """
        List current members who don't have a (resolved) inviter, with their id.

        :param context: The hybrid command context.
        """
        channel = await self._get_intro_channel(context)
        if channel is None:
            return
        await context.defer()
        try:
            await self._sync_channel(channel, EPOCH)
        except discord.Forbidden:
            await context.send(
                embed=discord.Embed(
                    title="Error!",
                    description=f"I can't read the history of {channel.mention}.",
                    color=0xE02B2B,
                )
            )
            return
        rows = await self._load_all(channel.id)
        intros, edges, _ = await self._resolve_invites(context.guild, rows)
        overrides = await self.bot.database.get_invite_overrides(context.guild.id)

        lines = []
        for member in sorted(
            context.guild.members, key=lambda m: m.display_name.casefold()
        ):
            if member.bot or edges.get(member.id) is not None:
                continue
            # Explain *why* there's no inviter, so the owner knows what to do:
            # post missing, intro silent, explicitly nobody, or unresolved name.
            if member.id in overrides:
                note = "set to nobody (override)"
            else:
                row = intros.get(member.id)
                if row is None:
                    note = "no intro found — fix with `/setinviter`"
                else:
                    raw = row["payload"].get("invited_by")
                    if raw is None:
                        note = "intro doesn't say — fix with `/setinviter`"
                    elif _norm(raw) in NONE_TOKENS:
                        note = "intro says nobody"
                    else:
                        clean = raw.replace("`", "")
                        note = f"says `{clean}` — unresolved, fix with `/identify {clean}`"
            lines.append(f"• **{member.display_name}** — `{member.id}` — {note}")

        if not lines:
            await context.send(
                embed=discord.Embed(
                    description="Everyone in the server has an inviter recorded. 🎉",
                    color=0xBEBEFE,
                )
            )
            return

        # Discord caps embed descriptions at 4096 chars; send in chunks.
        header = f"**{len(lines)}** member{'s' if len(lines) != 1 else ''} without an inviter:\n"
        chunks: list[str] = []
        current = header
        for line in lines:
            if len(current) + len(line) + 1 > 4000:
                chunks.append(current)
                current = ""
            current += line + "\n"
        chunks.append(current)
        for i, chunk in enumerate(chunks):
            await context.send(
                embed=discord.Embed(
                    title="📭 No inviter recorded" if i == 0 else None,
                    description=chunk,
                    color=0xBEBEFE,
                )
            )

    @commands.hybrid_command(
        name="identify",
        description="Teach the bot who an unresolved 'invited by' name refers to.",
    )
    @app_commands.describe(
        alias="The written name from the intro, e.g. 'gem'.",
        target="Ping the user it means, or write a name to use as a unique key.",
    )
    @commands.is_owner()
    @commands.guild_only()
    async def identify(self, context: Context, alias: str, *, target: str) -> None:
        """
        Map a written "invited by" name to a user (or to a stand-alone name key).

        Guardrails: an alias only has an effect if some intro actually *says*
        that name. If the given alias is really a member (a ping, or a name
        that no intro mentions but which uniquely matches a member), the owner
        almost certainly meant "set this member's inviter", so this behaves
        like /setinviter instead of silently storing a dead alias.

        :param context: The hybrid command context.
        :param alias: The written name from the intro message.
        :param target: A user mention/id, or a plain name used as a unique key.
        """
        await context.defer()
        alias_clean = alias.strip().strip("\"'“”‘’`")
        key = self._parse_target(target)
        if key.startswith("id:"):
            described = f"<@{key[3:]}>"
        else:
            described = f"the name key `{key[5:]}`"

        async def pin(member_id: int, why: str) -> None:
            inviter = None if _norm(target) in NONE_TOKENS else key
            await self.bot.database.set_invite_override(
                context.guild.id, member_id, inviter
            )
            await context.send(
                embed=discord.Embed(
                    description=(
                        f"{why}, so I treated this like `/setinviter`: "
                        f"<@{member_id}> is now recorded as invited by "
                        f"{'nobody' if inviter is None else described}.\n"
                        "Run `/invitemap` to see the updated map."
                    ),
                    color=0xBEBEFE,
                )
            )

        # A pinged "alias" is a member, not a written name.
        mention = MENTION_RE.fullmatch(alias_clean)
        if mention:
            await pin(int(mention.group(1)), "**That's a member ping**")
            return

        # Does any intro actually say this name? (Compare raw invited-by texts,
        # so this works whether the name is currently unresolved or not.)
        channel_id = await self.bot.database.get_intro_channel(context.guild.id)
        said_in_intros = False
        if channel_id is not None:
            rows = await self._load_all(channel_id)
            norm_alias = _norm(alias_clean)
            said_in_intros = any(
                _norm(row["payload"].get("invited_by") or "") == norm_alias
                for row in rows
            )

        if said_in_intros:
            await self.bot.database.set_invite_alias(
                context.guild.id, _norm(alias_clean), key
            )
            await context.send(
                embed=discord.Embed(
                    description=(
                        f"Got it — **{alias_clean}** now means {described}.\n"
                        "Run `/invitemap` to see the updated map."
                    ),
                    color=0xBEBEFE,
                )
            )
            return

        # No intro says this name. If it uniquely matches a member, the owner
        # meant to set that member's inviter.
        norm_alias = _norm(alias_clean)
        candidates = {
            member.id
            for member in context.guild.members
            if not member.bot
            and norm_alias
            in {
                _norm(member.display_name),
                _norm(member.name),
                _norm(member.global_name or ""),
            }
        }
        if len(candidates) == 1:
            await pin(
                next(iter(candidates)),
                f"**No intro says “{alias_clean}”, but it matches a member**",
            )
            return

        # Genuinely unknown: store the alias for the future, but say clearly
        # that it currently changes nothing.
        await self.bot.database.set_invite_alias(context.guild.id, norm_alias, key)
        await context.send(
            embed=discord.Embed(
                title="⚠️ Alias stored, but it won't change anything yet",
                description=(
                    f"No introduction currently says “{alias_clean}”, so this "
                    "alias has no effect on the map.\n\n"
                    "- To fix **who invited a member**, use "
                    "`/setinviter @member <inviter>`.\n"
                    "- To see the names that actually need identifying, use "
                    "`/whois`."
                ),
                color=0xE02B2B,
            )
        )

    @commands.hybrid_command(
        name="setinviter",
        description="Manually fix who invited a member (overrides their intro).",
    )
    @app_commands.describe(
        member="The member whose inviter to correct (mention or id).",
        inviter="Their real inviter: a mention/id, a name key, 'none', or 'auto' to un-pin.",
    )
    @commands.is_owner()
    @commands.guild_only()
    async def setinviter(
        self, context: Context, member: discord.User, *, inviter: str
    ) -> None:
        """
        Pin (or un-pin) a member's inviter, overriding their intro message.

        :param context: The hybrid command context.
        :param member: The member whose attribution to correct.
        :param inviter: A mention/id or name key; 'none' for nobody; 'auto' to
            remove the override and return to automatic resolution.
        """
        choice = inviter.strip().casefold()
        if choice == "auto":
            await self.bot.database.delete_invite_override(context.guild.id, member.id)
            description = f"{member.mention} is back to automatic attribution."
        elif choice in NONE_TOKENS:
            await self.bot.database.set_invite_override(
                context.guild.id, member.id, None
            )
            description = f"{member.mention} is now recorded as invited by nobody."
        else:
            key = self._parse_target(inviter)
            await self.bot.database.set_invite_override(
                context.guild.id, member.id, key
            )
            if key.startswith("id:"):
                described = f"<@{key[3:]}>"
            else:
                described = f"the name key `{key[5:]}`"
            description = f"{member.mention} is now recorded as invited by {described}."
        await context.send(
            embed=discord.Embed(
                description=f"{description}\nRun `/invitemap` to see the updated map.",
                color=0xBEBEFE,
            )
        )


async def setup(bot) -> None:
    await bot.add_cog(Introductions(bot))

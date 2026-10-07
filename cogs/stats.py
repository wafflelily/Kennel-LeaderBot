"""
/mystats — comparative personal stats.

The game sites already show plain personal stats (streaks, totals, and so on),
so this focuses on how a player stacks up against everyone else tallied in the
channel: days they had the top result, answers nobody else got, averages
versus the channel's, records held. Each leaderboard cog supplies its own
comparisons via ``compare_stats``; this command gathers and presents them.
"""

from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from leaderboard.base import LeaderboardCog, Trimmable, game_choices

# Discord's embed limits: characters per field value, and in total across the
# title, description, field names/values and footer.
FIELD_LIMIT = 1024
EMBED_LIMIT = 6000


def split_fields(name: str, lines: list) -> list[tuple[str, list[str], list[str]]]:
    """
    Split one game's stat lines into ``(field name, lines, trimmable lines)``.

    Discord doesn't render markdown headers inside embeds, so a cog marks a
    sub-header by starting a line with ``### ``; each becomes its own field
    name (which Discord styles like the game's own title), with the following
    lines as that field's value. A ``Trimmable`` element's lines go in the
    third slot, which ``fit_fields`` may shorten; they display after the
    field's ordinary lines.
    """
    fields: list[tuple[str, list[str], list[str]]] = []
    current_name, chunk, trimmable = name, [], []
    for line in lines:
        if isinstance(line, Trimmable):
            trimmable += line
        elif line.startswith("### "):
            if chunk or trimmable:
                fields.append((current_name, chunk, trimmable))
            current_name, chunk, trimmable = line[4:], [], []
        else:
            chunk.append(line)
    if chunk or trimmable:
        fields.append((current_name, chunk, trimmable))
    return fields


def _join(lines: list[str]) -> str:
    return "\n".join(lines)


def _trim(fixed: list[str], extra: list[str], limit: int) -> list[str]:
    """
    ``fixed`` plus as many of ``extra`` as fit in ``limit`` characters,
    ending with a "+N more" line for whatever was left out.
    """
    if len(_join(fixed + extra)) <= limit:
        return fixed + extra
    for keep in range(len(extra) - 1, -1, -1):
        lines = fixed + extra[:keep] + [f"+{len(extra) - keep} more"]
        if len(_join(lines)) <= limit:
            return lines
    return fixed + [f"+{len(extra)} more"]


def fit_fields(
    fields: list[tuple[str, list[str], list[str]]], used: int = 0
) -> list[tuple[str, str]]:
    """
    Turn ``split_fields`` output into ``(name, value)`` pairs that fit
    Discord's limits, shortening only the trimmable lists.

    ``used`` is what the rest of the embed (title, description, ...) already
    takes. The space left after every untrimmable part is shared between the
    trimmable lists smallest first: each gets an even share of what's left or
    its full size if that's less, and anything a short list doesn't need goes
    to the longer ones. So a short list stays whole while a long one is cut.
    """
    budget = EMBED_LIMIT - used
    for name, fixed, extra in fields:
        budget -= len(name)
        if not extra:
            budget -= len(_join(fixed))

    # Each trimmable field's allowance: its whole value, within the field limit.
    allowance: dict[int, int] = {}
    wanted = sorted(
        (len(_join(fixed + extra)), i)
        for i, (_, fixed, extra) in enumerate(fields)
        if extra
    )
    for n, (size, i) in enumerate(wanted):
        share = max(budget, 0) // (len(wanted) - n)
        allowance[i] = min(size, share, FIELD_LIMIT)
        budget -= len(_join(_trim(fields[i][1], fields[i][2], allowance[i])))

    return [
        (name, _join(_trim(fixed, extra, allowance[i]) if extra else fixed))
        for i, (name, fixed, extra) in enumerate(fields)
    ]


class Stats(commands.Cog, name="stats"):
    def __init__(self, bot) -> None:
        self.bot = bot

    def _games(self) -> dict[str, LeaderboardCog]:
        """The loaded leaderboard cogs that offer comparative stats."""
        return {
            cog.GAME: cog
            for cog in self.bot.cogs.values()
            if isinstance(cog, LeaderboardCog) and hasattr(cog, "compare_stats")
        }

    @commands.hybrid_command(
        name="mystats",
        description="How you stack up against this channel's other players.",
    )
    @app_commands.describe(
        game="Limit to one game, e.g. `gauntle`. Omit for every game tallied here.",
    )
    async def mystats(self, context: Context, *, game: str = None) -> None:
        """
        Show the invoker's comparative stats for the games tallied in this channel.

        :param context: The hybrid command context.
        :param game: Optional game to limit to; defaults to all of them.
        """
        games = self._games()
        if game is not None:
            game = game.strip().lower()
            if game not in games:
                valid = ", ".join(f"`{g}`" for g in sorted(games))
                await context.send(
                    embed=discord.Embed(
                        title="Error!",
                        description=f"Unknown game `{game}`.\nValid options: {valid}.",
                        color=0xE02B2B,
                    )
                )
                return
            games = {game: games[game]}

        await context.defer()

        fields = []
        for name, cog in sorted(games.items()):
            # Only games already tallied in this channel: reuse their scan
            # state for a cheap forward catch-up, never a surprise full scan.
            _, oldest_after = await self.bot.database.get_leaderboard_scan(
                name, context.channel.id
            )
            if oldest_after is None:
                continue
            try:
                await cog._sync_channel(
                    context.channel, datetime.fromisoformat(oldest_after)
                )
            except discord.Forbidden:
                continue
            rows = await cog._load_all(context.channel.id)
            lines = await cog.compare_stats(rows, context.author.id)
            if lines:
                fields.append((name.title(), lines))

        if not fields:
            await context.send(
                embed=discord.Embed(
                    title="📊 Your stats",
                    description=(
                        "No results of yours are tallied in this channel yet.\n\n"
                        "Post some results here, and run the game's leaderboard "
                        "command (e.g. `/gauntle`) once so I track this channel."
                    ),
                    color=0xE02B2B,
                )
            )
            return

        embed = discord.Embed(
            title=f"📊 Stats for {context.author.display_name}",
            description="Compared against everything tallied in this channel.",
            color=0xBEBEFE,
        )
        split = [
            sub
            for field_name, lines in fields
            for sub in split_fields(field_name, lines)
        ]
        # Long lists (e.g. Krillion catches) are shortened to fit the embed.
        for sub_name, value in fit_fields(split, used=len(embed)):
            embed.add_field(name=sub_name, value=value, inline=False)
        await context.send(embed=embed)

    @mystats.autocomplete("game")
    async def mystats_game_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return game_choices(self._games(), current)


async def setup(bot) -> None:
    await bot.add_cog(Stats(bot))

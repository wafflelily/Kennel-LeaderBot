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

from leaderboard.base import LeaderboardCog, game_choices


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
        for field_name, lines in fields:
            embed.add_field(name=field_name, value="\n".join(lines), inline=False)
        await context.send(embed=embed)

    @mystats.autocomplete("game")
    async def mystats_game_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return game_choices(self._games(), current)


async def setup(bot) -> None:
    await bot.add_cog(Stats(bot))

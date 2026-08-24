"""
Automatic month-end leaderboard posting.

Channels opt in per game with the owner-only /autopost command; a daily task
then posts the finished month's final leaderboard for every opted-in
(game, channel) pair on the 1st of the month, using the same embeds as the
manual commands (each leaderboard cog's ``build_leaderboard``).
"""

import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.commands import Context

from leaderboard.base import LeaderboardCog, game_choices

# When the daily check fires (UTC). Only the run on the 1st of a month posts;
# if the bot is offline at that moment the post for that month is skipped.
POST_AT = dt.time(hour=8, tzinfo=dt.timezone.utc)


class AutoPost(commands.Cog, name="autopost"):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.monthly_post.start()

    async def cog_unload(self) -> None:
        self.monthly_post.cancel()

    def _games(self) -> dict[str, LeaderboardCog]:
        """The loaded leaderboard cogs that can render a monthly board."""
        return {
            cog.GAME: cog
            for cog in self.bot.cogs.values()
            if isinstance(cog, LeaderboardCog) and hasattr(cog, "build_leaderboard")
        }

    @commands.hybrid_command(
        name="autopost",
        description="Post the final leaderboard here automatically on the 1st of each month.",
    )
    @app_commands.describe(
        state="`on` or `off`, or `status` to see this channel's current opt-ins.",
        game="Which leaderboard, e.g. `gauntle`. Omit for all of them.",
    )
    @app_commands.choices(
        state=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="status", value="status"),
        ]
    )
    @commands.is_owner()
    @commands.guild_only()
    async def autopost(self, context: Context, state: str, game: str = None) -> None:
        """
        Opt this channel in or out of automatic month-end leaderboard posts.

        :param context: The hybrid command context.
        :param state: "on", "off", or "status".
        :param game: Optional game to change; if omitted, every leaderboard.
        """
        games = self._games()
        state = state.strip().lower()

        if state == "status":
            enabled = sorted(
                g
                for g in await self.bot.database.get_autopost_games(context.channel.id)
                if g in games
            )
            if enabled:
                names = ", ".join(f"`{g}`" for g in enabled)
                description = f"This channel gets automatic monthly posts for {names}."
            else:
                description = "This channel has no automatic monthly posts."
            await context.send(
                embed=discord.Embed(description=description, color=0xBEBEFE)
            )
            return

        if state not in ("on", "off"):
            await context.send(
                embed=discord.Embed(
                    title="Error!",
                    description="State must be `on`, `off` or `status`.",
                    color=0xE02B2B,
                )
            )
            return

        if game is not None:
            game = game.strip().lower()
            if game not in games:
                valid = ", ".join(f"`{g}`" for g in sorted(games))
                await context.send(
                    embed=discord.Embed(
                        title="Error!",
                        description=f"Unknown leaderboard `{game}`.\nValid options: {valid}.",
                        color=0xE02B2B,
                    )
                )
                return
            targets = [game]
        else:
            targets = sorted(games)

        for name in targets:
            await self.bot.database.set_autopost(
                name, context.channel.id, state == "on"
            )

        names = ", ".join(f"`{name}`" for name in targets)
        if state == "on":
            description = (
                f"On the 1st of each month I'll post the previous month's final "
                f"{names} leaderboard{'s' if len(targets) != 1 else ''} in this channel."
            )
        else:
            description = f"Automatic monthly posts for {names} are off in this channel."
        await context.send(
            embed=discord.Embed(
                title="🗓️ Monthly auto-post", description=description, color=0xBEBEFE
            )
        )

    @autopost.autocomplete("game")
    async def autopost_game_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return game_choices(self._games(), current)

    @tasks.loop(time=POST_AT)
    async def monthly_post(self) -> None:
        """Runs daily; on the 1st, posts last month's boards to opted-in channels."""
        if dt.datetime.now(dt.timezone.utc).day != 1:
            return
        # On the 1st, the default window is exactly the month that just ended.
        after, label, month_filter = LeaderboardCog._resolve_window(None)
        for game, cog in self._games().items():
            for channel_id in await self.bot.database.get_autopost_channels(game):
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    continue
                try:
                    await cog._sync_channel(channel, after - cog.SCAN_BUFFER)
                    embed = await cog.build_leaderboard(channel, month_filter, label)
                    if embed is not None:
                        await channel.send(embed=embed)
                except discord.Forbidden:
                    self.bot.logger.warning(
                        f"Auto-post: missing permissions for {game} "
                        f"in channel {channel_id}"
                    )
                except Exception:
                    self.bot.logger.error(
                        f"Auto-post failed for {game} in channel {channel_id}",
                        exc_info=True,
                    )

    @monthly_post.before_loop
    async def before_monthly_post(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(AutoPost(bot))

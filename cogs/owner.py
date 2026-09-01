"""
Copyright © Krypton 2019-Present - https://github.com/kkrypt0nn (https://krypton.ninja)
Description:
🐍 A simple template to start to code your own and personalized Discord bot in Python

Version: 6.5.0
"""

from typing import Union

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from leaderboard.base import LeaderboardCog, game_choices


class Owner(commands.Cog, name="owner"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.command(
        name="sync",
        description="Synchonizes the slash commands.",
    )
    @app_commands.describe(scope="The scope of the sync. Can be `global` or `guild`")
    @commands.is_owner()
    async def sync(self, context: Context, scope: str) -> None:
        """
        Synchonizes the slash commands.

        :param context: The command context.
        :param scope: The scope of the sync. Can be `global` or `guild`.
        """

        if scope == "global":
            await context.bot.tree.sync()
            embed = discord.Embed(
                description="Slash commands have been globally synchronized.",
                color=0xBEBEFE,
            )
            await context.send(embed=embed)
            return
        elif scope == "guild":
            context.bot.tree.copy_global_to(guild=context.guild)
            await context.bot.tree.sync(guild=context.guild)
            embed = discord.Embed(
                description="Slash commands have been synchronized in this guild.",
                color=0xBEBEFE,
            )
            await context.send(embed=embed)
            return
        embed = discord.Embed(
            description="The scope must be `global` or `guild`.", color=0xE02B2B
        )
        await context.send(embed=embed)

    @commands.command(
        name="unsync",
        description="Unsynchonizes the slash commands.",
    )
    @app_commands.describe(
        scope="The scope of the sync. Can be `global`, `current_guild` or `guild`"
    )
    @commands.is_owner()
    async def unsync(self, context: Context, scope: str) -> None:
        """
        Unsynchonizes the slash commands.

        :param context: The command context.
        :param scope: The scope of the sync. Can be `global`, `current_guild` or `guild`.
        """

        if scope == "global":
            context.bot.tree.clear_commands(guild=None)
            await context.bot.tree.sync()
            embed = discord.Embed(
                description="Slash commands have been globally unsynchronized.",
                color=0xBEBEFE,
            )
            await context.send(embed=embed)
            return
        elif scope == "guild":
            context.bot.tree.clear_commands(guild=context.guild)
            await context.bot.tree.sync(guild=context.guild)
            embed = discord.Embed(
                description="Slash commands have been unsynchronized in this guild.",
                color=0xBEBEFE,
            )
            await context.send(embed=embed)
            return
        embed = discord.Embed(
            description="The scope must be `global` or `guild`.", color=0xE02B2B
        )
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="load",
        description="Load a cog",
    )
    @app_commands.describe(cog="The name of the cog to load")
    @commands.is_owner()
    async def load(self, context: Context, cog: str) -> None:
        """
        The bot will load the given cog.

        :param context: The hybrid command context.
        :param cog: The name of the cog to load.
        """
        try:
            await self.bot.load_extension(f"cogs.{cog}")
        except Exception:
            embed = discord.Embed(
                description=f"Could not load the `{cog}` cog.", color=0xE02B2B
            )
            await context.send(embed=embed)
            return
        embed = discord.Embed(
            description=f"Successfully loaded the `{cog}` cog.", color=0xBEBEFE
        )
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="unload",
        description="Unloads a cog.",
    )
    @app_commands.describe(cog="The name of the cog to unload")
    @commands.is_owner()
    async def unload(self, context: Context, cog: str) -> None:
        """
        The bot will unload the given cog.

        :param context: The hybrid command context.
        :param cog: The name of the cog to unload.
        """
        try:
            await self.bot.unload_extension(f"cogs.{cog}")
        except Exception:
            embed = discord.Embed(
                description=f"Could not unload the `{cog}` cog.", color=0xE02B2B
            )
            await context.send(embed=embed)
            return
        embed = discord.Embed(
            description=f"Successfully unloaded the `{cog}` cog.", color=0xBEBEFE
        )
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="reload",
        description="Reloads a cog.",
    )
    @app_commands.describe(cog="The name of the cog to reload")
    @commands.is_owner()
    async def reload(self, context: Context, cog: str) -> None:
        """
        The bot will reload the given cog.

        :param context: The hybrid command context.
        :param cog: The name of the cog to reload.
        """
        try:
            await self.bot.reload_extension(f"cogs.{cog}")
        except Exception:
            embed = discord.Embed(
                description=f"Could not reload the `{cog}` cog.", color=0xE02B2B
            )
            await context.send(embed=embed)
            return
        embed = discord.Embed(
            description=f"Successfully reloaded the `{cog}` cog.", color=0xBEBEFE
        )
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="rebuild",
        description="Clear cached leaderboard data so it's re-scanned and re-parsed on next use.",
    )
    @app_commands.describe(
        game="Which leaderboard to reset, e.g. `gauntle`. Omit to reset all of them.",
    )
    @commands.is_owner()
    async def rebuild(self, context: Context, game: str = None) -> None:
        """
        Invalidate the leaderboard cache after the parsing logic changes.

        Deletes the cached results and scan state so the next time each
        leaderboard command runs in a channel it re-scans that channel's history
        and re-parses every result under the current rules.

        :param context: The hybrid command context.
        :param game: Optional game to reset; if omitted, every leaderboard is reset.
        """
        # Discover the loaded leaderboard cogs rather than hardcoding names.
        games = {
            cog.GAME: cog
            for cog in self.bot.cogs.values()
            if isinstance(cog, LeaderboardCog)
        }
        if not games:
            await context.send(
                embed=discord.Embed(
                    description="No leaderboard cogs are loaded.", color=0xE02B2B
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

        removed = 0
        for name in targets:
            removed += await self.bot.database.clear_leaderboard_game(name)

        names = ", ".join(f"`{name}`" for name in targets)
        await context.send(
            embed=discord.Embed(
                title="🔄 Leaderboard cache cleared",
                description=(
                    f"Cleared cached data for {names} ({removed} result"
                    f"{'s' if removed != 1 else ''} removed).\n\n"
                    "The next time each command runs in a channel it will re-scan "
                    "that channel's history and re-parse everything under the "
                    "current rules."
                ),
                color=0xBEBEFE,
            )
        )

    @rebuild.autocomplete("game")
    async def rebuild_game_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        games = {
            cog.GAME
            for cog in self.bot.cogs.values()
            if isinstance(cog, LeaderboardCog)
        }
        return game_choices(games, current)

    @staticmethod
    async def _import_history(
        cog: LeaderboardCog, source, target_channel_id: int
    ) -> int:
        """
        Scan ``source``'s full history once and store every result ``cog``
        recognises under ``target_channel_id`` instead of the source channel.
        Returns how many results were imported. Results are keyed by message
        id, so re-importing updates rather than duplicates.
        """
        imported = 0
        async for message in source.history(limit=None, oldest_first=True):
            if message.author.bot or not message.content:
                continue
            parsed = cog.parse(message.content, message.created_at.date())
            if parsed is None:
                continue
            played_on, payload = parsed
            await cog.bot.database.upsert_leaderboard_result(
                cog.GAME,
                target_channel_id,
                message.id,
                message.author.id,
                message.author.display_name,
                played_on,
                payload,
            )
            imported += 1
        return imported

    @commands.hybrid_command(
        name="import",
        description="One-off scan of another channel, merging a game's results into this channel.",
    )
    @app_commands.describe(
        game="Which leaderboard to import, e.g. `gauntle`.",
        channel="The channel whose history should be scanned.",
    )
    @commands.is_owner()
    @commands.guild_only()
    async def import_results(
        self,
        context: Context,
        game: str,
        channel: Union[discord.TextChannel, discord.Thread],
    ) -> None:
        """
        Import another channel's results for a game into this channel's data.

        Useful when results used to be posted somewhere else: the source
        channel's history is scanned once and every result found is stored
        against *this* channel, merging into its leaderboards and stats.
        Re-running is safe (results are keyed by message, nothing duplicates),
        but it's a one-off — new results in the source channel afterwards are
        not tracked, and `/rebuild` wipes imported rows along with the rest.

        :param context: The hybrid command context.
        :param game: The leaderboard game to import results for.
        :param channel: The channel to scan.
        """
        games = {
            cog.GAME: cog
            for cog in self.bot.cogs.values()
            if isinstance(cog, LeaderboardCog)
        }
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
        if channel.id == context.channel.id:
            await context.send(
                embed=discord.Embed(
                    title="Error!",
                    description=(
                        "That's this channel — pick the *other* channel the "
                        "results should be imported from."
                    ),
                    color=0xE02B2B,
                )
            )
            return

        # Scanning a whole channel's history can take a while.
        await context.defer()

        try:
            imported = await self._import_history(
                games[game], channel, context.channel.id
            )
        except discord.Forbidden:
            await context.send(
                embed=discord.Embed(
                    title="Error!",
                    description=(
                        f"I don't have permission to read the history of "
                        f"{channel.mention}.\n\nPlease give me the **View Channel** "
                        "and **Read Message History** permissions there, then try again."
                    ),
                    color=0xE02B2B,
                )
            )
            return

        await context.send(
            embed=discord.Embed(
                title="📥 Import complete",
                description=(
                    f"Imported **{imported}** `{game}` result"
                    f"{'s' if imported != 1 else ''} from {channel.mention} into "
                    "this channel's data.\n\nThis was a one-off: new results "
                    f"posted in {channel.mention} won't be tracked here."
                ),
                color=0xBEBEFE,
            )
        )

    @import_results.autocomplete("game")
    async def import_game_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        games = {
            cog.GAME
            for cog in self.bot.cogs.values()
            if isinstance(cog, LeaderboardCog)
        }
        return game_choices(games, current)

    @commands.hybrid_command(
        name="shutdown",
        description="Make the bot shutdown.",
    )
    @commands.is_owner()
    async def shutdown(self, context: Context) -> None:
        """
        Shuts down the bot.

        :param context: The hybrid command context.
        """
        embed = discord.Embed(description="Shutting down. Bye! :wave:", color=0xBEBEFE)
        await context.send(embed=embed)
        await self.bot.close()

    @commands.hybrid_command(
        name="say",
        description="The bot will say anything you want.",
    )
    @app_commands.describe(message="The message that should be repeated by the bot")
    @commands.is_owner()
    async def say(self, context: Context, *, message: str) -> None:
        """
        The bot will say anything you want.

        :param context: The hybrid command context.
        :param message: The message that should be repeated by the bot.
        """
        await context.send(message)

    @commands.hybrid_command(
        name="embed",
        description="The bot will say anything you want, but within embeds.",
    )
    @app_commands.describe(message="The message that should be repeated by the bot")
    @commands.is_owner()
    async def embed(self, context: Context, *, message: str) -> None:
        """
        The bot will say anything you want, but using embeds.

        :param context: The hybrid command context.
        :param message: The message that should be repeated by the bot.
        """
        embed = discord.Embed(description=message, color=0xBEBEFE)
        await context.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(Owner(bot))

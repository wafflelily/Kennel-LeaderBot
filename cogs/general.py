"""
Copyright © Krypton 2019-Present - https://github.com/kkrypt0nn (https://krypton.ninja)
Description:
🐍 A simple template to start to code your own and personalized Discord bot in Python

Version: 6.5.0
"""

import platform
import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context


class FeedbackForm(discord.ui.Modal, title="Feeedback"):
    feedback = discord.ui.TextInput(
        label="What do you think about this bot?",
        style=discord.TextStyle.long,
        placeholder="Type your answer here...",
        required=True,
        max_length=256,
    )

    async def on_submit(self, interaction: discord.Interaction):
        self.interaction = interaction
        self.answer = str(self.feedback)
        self.stop()


class General(commands.Cog, name="general"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.context_menu_user = app_commands.ContextMenu(
            name="Grab ID", callback=self.grab_id
        )
        self.bot.tree.add_command(self.context_menu_user)
        self.context_menu_message = app_commands.ContextMenu(
            name="Remove spoilers", callback=self.remove_spoilers
        )
        self.bot.tree.add_command(self.context_menu_message)

    # Message context menu command
    async def remove_spoilers(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        """
        Removes the spoilers from the message. This command requires the MESSAGE_CONTENT intent to work properly.

        :param interaction: The application command interaction.
        :param message: The message that is being interacted with.
        """
        spoiler_attachment = None
        for attachment in message.attachments:
            if attachment.is_spoiler():
                spoiler_attachment = attachment
                break
        embed = discord.Embed(
            title="Message without spoilers",
            description=message.content.replace("||", ""),
            color=0xBEBEFE,
        )
        if spoiler_attachment is not None:
            embed.set_image(url=attachment.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # User context menu command
    async def grab_id(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        """
        Grabs the ID of the user.

        :param interaction: The application command interaction.
        :param user: The user that is being interacted with.
        """
        embed = discord.Embed(
            description=f"The ID of {user.mention} is `{user.id}`.",
            color=0xBEBEFE,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.hybrid_command(
        name="help", description="List all commands the bot has loaded."
    )
    async def help(self, context: Context) -> None:
        embed = discord.Embed(
            title="Help", description="List of available commands:", color=0xBEBEFE
        )
        for i in self.bot.cogs:
            if i == "owner" and not (await self.bot.is_owner(context.author)):
                continue
            cog = self.bot.get_cog(i.lower())
            commands = cog.get_commands()
            data = []
            for command in commands:
                description = command.description.partition("\n")[0]
                data.append(f"{command.name} - {description}")
            help_text = "\n".join(data)
            embed.add_field(
                name=i.capitalize(), value=f"```{help_text}```", inline=False
            )
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="botinfo",
        description="Get some useful (or not) information about the bot.",
    )
    async def botinfo(self, context: Context) -> None:
        """
        Get some useful (or not) information about the bot.

        :param context: The hybrid command context.
        """
        embed = discord.Embed(
            description="Used [Krypton's](https://krypton.ninja) template",
            color=0xBEBEFE,
        )
        embed.set_author(name="Bot Information")
        embed.add_field(name="Owner:", value="batterysnek", inline=True)
        embed.add_field(
            name="Python Version:", value=f"{platform.python_version()}", inline=True
        )
        embed.add_field(
            name="Prefix:",
            value=f"/ (Slash Commands) or {self.bot.bot_prefix} for normal commands",
            inline=False,
        )
        embed.set_footer(text=f"Requested by {context.author}")
        await context.send(embed=embed)


    @commands.hybrid_command(
        name="ping",
        description="Check if the bot is alive.",
    )
    async def ping(self, context: Context) -> None:
        """
        Check if the bot is alive.

        :param context: The hybrid command context.
        """
        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"The bot latency is {round(self.bot.latency * 1000)}ms.",
            color=0xBEBEFE,
        )
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="invite",
        description="Get the invite link of the bot to be able to invite it.",
    )
    async def invite(self, context: Context) -> None:
        """
        Get the invite link of the bot to be able to invite it.

        :param context: The hybrid command context.
        """
        embed = discord.Embed(
            description=f"Invite me by clicking [here]({self.bot.invite_link}).",
            color=0xD75BF4,
        )
        try:
            await context.author.send(embed=embed)
            await context.send("I sent you a private message!")
        except discord.Forbidden:
            await context.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(General(bot))

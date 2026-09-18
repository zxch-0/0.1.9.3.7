"""``/home`` — the single entry point of the whole flow."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .client import FormBot
from .views import home_embed, home_view

log = logging.getLogger("formbot.cog_home")


class Home(commands.Cog):
    """Shows the card with the big START button; the button opens the modal."""

    def __init__(self, bot: FormBot) -> None:
        self.bot = bot

    @app_commands.command(name="home", description="Open the form (big START button → infos → admin gets a DM)")
    @app_commands.checks.cooldown(2, 20)
    async def home(self, interaction: discord.Interaction) -> None:
        cfg, texts = self.bot.cfg, self.bot.texts
        user = self.bot.user
        avatar = str(user.display_avatar.url) if user is not None else None

        embed = home_embed(cfg, texts, page=0, avatar_url=avatar)
        view = home_view(cfg, texts, page=0, avatar_url=avatar)
        # Ephemeral: only the member who ran the command sees the card.
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @home.error  # type: ignore[attr-defined]
    async def _home_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            remaining = max(1, int(error.retry_after + 0.999))
            await interaction.response.send_message(
                self.bot.texts.on_cooldown.format(seconds=self.bot.cfg.cooldown_seconds, remaining=remaining),
                ephemeral=True,
            )
            return
        log.warning("/home failed: %s", error)
        await self.bot.tree.on_error(interaction, error)  # one consistent fallback everywhere


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Home(bot))

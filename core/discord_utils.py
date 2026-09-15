import discord
from discord import app_commands

from core.config import env_optional_int


def get_dev_guild_id():
    return env_optional_int("DEV_GUILD_ID")


def guilds_decorator():
    dev_guild_id = get_dev_guild_id()
    return app_commands.guilds(discord.Object(id=dev_guild_id)) if dev_guild_id else (lambda f: f)

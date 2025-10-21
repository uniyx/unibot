# cogs/tweets.py
import os
import re
import json
import random
import pathlib
import time
from typing import List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

# Configure via env:
# DEV_GUILD_ID         → your dev server for instant slash visibility
# TWEETS_JS_PATH       → absolute or relative path to tweets.js (archive file)
# TWITTER_USERNAME     → your handle without @ (e.g., uniyx)
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None
GUILDS = app_commands.guilds(discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else (lambda f: f)

TWEETS_JS_PATH = os.getenv("TWEETS_JS_PATH", "data/tweets.js")
TWITTER_USERNAME = os.getenv("TWITTER_USERNAME", "").strip()

_ASSIGNMENT_PREFIX = re.compile(r"^[^{\[]*=\s*", re.DOTALL)

def _extract_array_from_js(js_text: str) -> List[dict]:
    """
    Strips the leading 'window.YTD.tweets.partN = ' and returns the parsed JSON array.
    Your archive may contain 'part0', 'part1', etc. If you later concatenate files,
    this function still expects a single JSON array in the current file.
    """
    payload = _ASSIGNMENT_PREFIX.sub("", js_text).strip()
    return json.loads(payload)

def _get_tweet_obj(obj: dict) -> Optional[dict]:
    # Archives typically wrap each entry in {"tweet": {...}}; tolerate plain objects too.
    if not isinstance(obj, dict):
        return None
    tw = obj.get("tweet") if "tweet" in obj else obj
    return tw if isinstance(tw, dict) else None

def _collect_id_and_text(items: List[dict]) -> List[Tuple[str, str]]:
    """
    Returns a list of (id_str, full_text_lower) for efficient filtering.
    Falls back through common text fields used by Twitter archives.
    """
    pairs: List[Tuple[str, str]] = []
    for obj in items:
        tw = _get_tweet_obj(obj)
        if not tw:
            continue
        tid = tw.get("id_str") or tw.get("id")
        if tid is None:
            continue
        # Prefer full_text if present, else text; normalize to str
        text = tw.get("full_text") or tw.get("text") or ""
        # Some archives store display text in "extended_tweet" as well
        if not text and isinstance(tw.get("extended_tweet"), dict):
            text = tw["extended_tweet"].get("full_text") or ""
        pairs.append((str(tid), str(text).lower()))
    return pairs

class TweetArchive:
    """
    Lazy loader with mtime-based cache invalidation. Keeps memory footprint small,
    avoids reparsing on every command.
    """
    def __init__(self, path: str):
        self.path = pathlib.Path(path)
        self._pairs: List[Tuple[str, str]] = []  # (id_str, text_lower)
        self._mtime: Optional[float] = None

    def _needs_reload(self) -> bool:
        try:
            m = self.path.stat().st_mtime
        except FileNotFoundError:
            return True
        return self._mtime is None or m > self._mtime or not self._pairs

    def _load(self) -> None:
        text = self.path.read_text(encoding="utf-8")
        arr = _extract_array_from_js(text)
        pairs = _collect_id_and_text(arr)
        # Record new state only if parse succeeded
        self._pairs = pairs
        self._mtime = self.path.stat().st_mtime

    def ensure_loaded(self) -> None:
        if self._needs_reload():
            self._load()

    def _all_ids(self) -> List[str]:
        self.ensure_loaded()
        return [tid for tid, _ in self._pairs]

    def _ids_matching_keyword(self, keyword: str) -> List[str]:
        self.ensure_loaded()
        k = keyword.strip().lower()
        if not k:
            return self._all_ids()
        return [tid for tid, txt in self._pairs if k in txt]

    def random_id(self, keyword: Optional[str] = None) -> str:
        """
        If keyword is provided, choose uniformly from tweets whose text contains it (case-insensitive).
        Otherwise choose uniformly from the entire archive.
        """
        self.ensure_loaded()
        pool = self._all_ids() if not keyword else self._ids_matching_keyword(keyword)
        if not pool:
            raise RuntimeError(f"No tweets matched keyword '{keyword}' in {self.path}")
        return random.choice(pool)

class TweetsCog(commands.Cog):
    """
    /random_tweet → posts a random FXTwitter URL from your archive
    /reload_tweets → force reload the archive file
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not TWITTER_USERNAME:
            raise RuntimeError("Set TWITTER_USERNAME in your environment")
        self.archive = TweetArchive(TWEETS_JS_PATH)

    @GUILDS
    @app_commands.command(
        name="random_tweet",
        description="Post a random tweet from my archive. Optionally filter by keyword."
    )
    @app_commands.describe(
        keyword="Optional keyword to search within tweet text, e.g., 'dog'"
    )
    async def random_tweet(self, interaction: discord.Interaction, keyword: Optional[str] = None):
        try:
            tid = self.archive.random_id(keyword=keyword)
            url = f"https://fxtwitter.com/{TWITTER_USERNAME}/status/{tid}"
            await interaction.response.send_message(url)
        except Exception as e:
            await interaction.response.send_message(
                f"Could not pick a tweet: {e}", ephemeral=True
            )

    @GUILDS
    @app_commands.command(name="reload_tweets", description="Reload tweets.js from disk")
    async def reload_tweets(self, interaction: discord.Interaction):
        try:
            # Force a reload by resetting cache and calling ensure_loaded
            self.archive._mtime = None
            self.archive.ensure_loaded()
            await interaction.response.send_message(
                f"Reloaded archive from `{self.archive.path}` "
                f"({len(self.archive._all_ids())} IDs).",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.response.send_message(f"Reload failed: {e}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(TweetsCog(bot))

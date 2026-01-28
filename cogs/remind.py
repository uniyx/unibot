# cogs/remind.py

import os
import re
import asyncio
import sqlite3
import datetime as dt
from typing import Optional, Tuple, List, Sequence, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

# =========================
# CONFIG
# =========================

DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None

TEAM_GUILD_ID = int(os.getenv("TEAM_GUILD_ID", "0")) or None  # recommended if bot is in multiple guilds
TEAM_ROLE_ID = int(os.getenv("TEAM_ROLE_ID", "0"))
TEAM_CHANNEL_ID = int(os.getenv("TEAM_CHANNEL_ID", "0"))

REMIND_DB_PATH = os.getenv("REMIND_DB_PATH", "data/reminders.sqlite3")
REMIND_TZ = os.getenv("REMIND_TZ", "America/New_York")
REMIND_POLL_SECONDS = int(os.getenv("REMIND_POLL_SECONDS", "10"))

REMIND_EMBED_COLOR = os.getenv("REMIND_EMBED_COLOR", "#5865F2")

LEAD_SECONDS = 30 * 60  # 30 minutes


def guilds_decorator():
    return app_commands.guilds(discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else (lambda f: f)


# =========================
# HELPERS
# =========================

class RemindError(RuntimeError):
    pass


def _require_config() -> None:
    missing = []
    if TEAM_ROLE_ID == 0:
        missing.append("TEAM_ROLE_ID")
    if TEAM_CHANNEL_ID == 0:
        missing.append("TEAM_CHANNEL_ID")
    if missing:
        raise RemindError(f"Missing required env vars: {', '.join(missing)}")


def _ensure_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            created_by INTEGER NOT NULL,
            remind_at_utc INTEGER NOT NULL,
            message TEXT NOT NULL,
            created_at_utc INTEGER NOT NULL,
            pre_sent INTEGER NOT NULL DEFAULT 0,
            main_sent INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    cols = {row[1] for row in conn.execute("PRAGMA table_info(reminders)").fetchall()}
    if "pre_sent" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN pre_sent INTEGER NOT NULL DEFAULT 0")
    if "main_sent" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN main_sent INTEGER NOT NULL DEFAULT 0")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_remind_at ON reminders(remind_at_utc)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pre_sent ON reminders(pre_sent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_main_sent ON reminders(main_sent)")
    conn.commit()


def _now_utc_ts() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def _get_zoneinfo(tz_name: str) -> dt.tzinfo:
    from zoneinfo import ZoneInfo
    return ZoneInfo(tz_name)


_LOCAL_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})\s*$")


def parse_when_to_utc(when_raw: str, tz_name: str) -> Tuple[dt.datetime, dt.datetime]:
    tz = _get_zoneinfo(tz_name)
    s = (when_raw or "").strip()
    if not s:
        raise RemindError("Missing time. Use: YYYY-MM-DD HH:MM (local)")

    m = _LOCAL_RE.match(s)
    if m:
        y, mo, d, hh, mm = map(int, m.groups())
        when_local = dt.datetime(y, mo, d, hh, mm, tzinfo=tz)
        when_utc = when_local.astimezone(dt.timezone.utc)
        return when_local, when_utc

    try:
        iso = s.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(iso)
    except Exception:
        raise RemindError(
            "Invalid time format. Use `YYYY-MM-DD HH:MM` (example: `2026-01-28 19:30`) "
            "or ISO 8601 (example: `2026-01-28T19:30-05:00`)."
        )

    if parsed.tzinfo is None:
        when_local = parsed.replace(tzinfo=tz)
        when_utc = when_local.astimezone(dt.timezone.utc)
        return when_local, when_utc

    when_utc = parsed.astimezone(dt.timezone.utc)
    when_local = when_utc.astimezone(tz)
    return when_local, when_utc


def member_has_role(member: discord.abc.User, role_id: int) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return any(r.id == role_id for r in member.roles)


def _preview_message(msg: str, max_len: int = 180) -> str:
    s = (msg or "").strip().replace("\n", " ")
    if len(s) > max_len:
        return s[:max_len] + "…"
    return s


def _embed_color() -> discord.Color:
    try:
        return discord.Color.from_str(REMIND_EMBED_COLOR)
    except Exception:
        return discord.Color.blurple()


# =========================
# UI: Cancel buttons for /reminders
# =========================

class RemindersCancelView(discord.ui.View):
    """
    Non-persistent view (buttons stop working after bot restart).
    Each button cancels one reminder id.
    """

    def __init__(self, cog: "Remind", guild_id: int, limit: int, rows: Sequence[tuple]) -> None:
        super().__init__(timeout=15 * 60)  # 15 minutes is plenty; prevents zombie UIs.
        self.cog = cog
        self.guild_id = guild_id
        self.limit = limit

        # rows: (id, remind_at_utc, message, created_by, pre_sent, main_sent)
        # Discord caps components: 5 rows * 5 buttons = 25
        for idx, r in enumerate(rows[:25], start=1):
            rid = int(r[0])
            created_by = int(r[3])

            self.add_item(ReminderCancelButton(index=idx, reminder_id=rid, created_by=created_by))


class ReminderCancelButton(discord.ui.Button):
    def __init__(self, *, index: int, reminder_id: int, created_by: int) -> None:
        super().__init__(
            label=f"Cancel {index}",
            style=discord.ButtonStyle.danger,
        )
        self.reminder_id = reminder_id
        self.created_by = created_by

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = self.view.cog  # type: ignore[attr-defined]

        # Permission: only creator can cancel.
        if interaction.user.id != self.created_by:
            await interaction.response.send_message(
                "You can only cancel reminders that you created.",
                ephemeral=True,
            )
            return

        try:
            await cog._cancel_reminder_by_id(interaction, self.reminder_id)

            # Refresh the message embed + buttons after cancellation.
            new_rows = await cog._fetch_upcoming_rows(guild_id=self.view.guild_id, limit=self.view.limit)  # type: ignore[attr-defined]
            new_embed = cog._build_reminders_list_embed(
                rows=new_rows,
                requested_by=interaction.user.id,
                limit=self.view.limit,  # type: ignore[attr-defined]
            )
            new_view = RemindersCancelView(
                cog=cog,
                guild_id=self.view.guild_id,  # type: ignore[attr-defined]
                limit=self.view.limit,        # type: ignore[attr-defined]
                rows=new_rows,
            )

            # If user clicks very fast, the original interaction might already be responded to.
            if interaction.response.is_done():
                await interaction.followup.send("Cancelled.", ephemeral=True)
            else:
                await interaction.response.send_message("Cancelled.", ephemeral=True)

            # Edit the original /reminders message to reflect the updated list.
            try:
                await interaction.message.edit(embed=new_embed, view=new_view)
            except Exception:
                # Editing can fail if message is gone or perms changed; cancellation already succeeded.
                pass

        except RemindError as e:
            if interaction.response.is_done():
                await interaction.followup.send(str(e), ephemeral=True)
            else:
                await interaction.response.send_message(str(e), ephemeral=True)
        except Exception:
            if interaction.response.is_done():
                await interaction.followup.send("Unexpected error cancelling that reminder.", ephemeral=True)
            else:
                await interaction.response.send_message("Unexpected error cancelling that reminder.", ephemeral=True)


# =========================
# COG
# =========================

class Remind(commands.Cog):
    """
    /remind: schedule a reminder (plus 30-minute lead reminder)
    /reminders: post upcoming reminders to the team channel, with creator-cancel buttons
    """

    def __init__(self, bot: commands.Bot) -> None:
        _require_config()

        self.bot = bot
        self.tz = _get_zoneinfo(REMIND_TZ)
        self.lock = asyncio.Lock()

        if os.path.dirname(REMIND_DB_PATH):
            os.makedirs(os.path.dirname(REMIND_DB_PATH), exist_ok=True)

        self.conn = sqlite3.connect(REMIND_DB_PATH, check_same_thread=False)
        _ensure_db(self.conn)

        self.remind_poller.change_interval(seconds=max(2, REMIND_POLL_SECONDS))
        self.remind_poller.start()

    def cog_unload(self) -> None:
        self.remind_poller.cancel()
        try:
            self.conn.close()
        except Exception:
            pass

    async def _guard(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            raise RemindError("This command must be used in a server.")
        if TEAM_GUILD_ID and interaction.guild.id != TEAM_GUILD_ID:
            raise RemindError("This command can only be used in the team server.")
        if not member_has_role(interaction.user, TEAM_ROLE_ID):
            raise RemindError("You do not have permission to use this command.")

    # ---------- DB helpers used by the View ----------
    async def _fetch_upcoming_rows(self, *, guild_id: int, limit: int) -> List[tuple]:
        now_ts = _now_utc_ts()
        async with self.lock:
            return self.conn.execute(
                """
                SELECT id, remind_at_utc, message, created_by, pre_sent, main_sent
                FROM reminders
                WHERE guild_id = ? AND remind_at_utc > ? AND main_sent = 0
                ORDER BY remind_at_utc ASC
                LIMIT ?
                """,
                (guild_id, now_ts, int(limit)),
            ).fetchall()

    async def _cancel_reminder_by_id(self, interaction: discord.Interaction, reminder_id: int) -> None:
        """
        Cancel only if the reminder exists, belongs to this guild, and was created by the caller.
        """
        if interaction.guild is None:
            raise RemindError("Server context missing.")

        async with self.lock:
            row = self.conn.execute(
                """
                SELECT id, created_by, remind_at_utc
                FROM reminders
                WHERE id = ? AND guild_id = ? AND main_sent = 0
                """,
                (int(reminder_id), interaction.guild.id),
            ).fetchone()

            if not row:
                raise RemindError("That reminder no longer exists (or was already sent).")

            _rid, created_by, remind_at_utc = row
            if int(created_by) != interaction.user.id:
                raise RemindError("You can only cancel reminders that you created.")

            self.conn.execute("DELETE FROM reminders WHERE id = ?", (int(reminder_id),))
            self.conn.commit()

    # ---------- Embed builders ----------
    def _build_embed(
        self,
        *,
        title: str,
        message: str,
        scheduled_ts: int,
        created_by: int,
        footer_hint: Optional[str] = None,
    ) -> discord.Embed:
        desc = message.strip() if (message or "").strip() else "(no message)"
        embed = discord.Embed(title=title, description=desc, color=_embed_color())
        embed.add_field(name="When", value=f"<t:{scheduled_ts}:f>\n<t:{scheduled_ts}:R>", inline=True)
        embed.add_field(name="Scheduled by", value=f"<@{created_by}>", inline=True)
        if footer_hint:
            embed.set_footer(text=footer_hint)
        return embed

    def _build_reminders_list_embed(
        self,
        *,
        rows: Sequence[tuple],
        requested_by: int,
        limit: int,
    ) -> discord.Embed:
        embed = discord.Embed(title="Upcoming Team Reminders", color=_embed_color())

        if not rows:
            embed.description = "No upcoming reminders scheduled."
            embed.set_footer(text=f"Requested by <@{requested_by}>")
            return embed

        lines: List[str] = []
        for idx, (_rid, remind_at_utc, msg, created_by, pre_sent, _main_sent) in enumerate(rows[:25], start=1):
            ts = int(remind_at_utc)
            lead_ts = ts - LEAD_SECONDS
            lead_status = "sent" if int(pre_sent) == 1 else "pending"
            preview = _preview_message(msg, max_len=160)

            lines.append(
                f"**{idx}.** <t:{ts}:R> (<t:{ts}:f>)\n"
                f"Lead: <t:{lead_ts}:t> ({lead_status})\n"
                f"By: <@{int(created_by)}>\n"
                f"{preview}"
            )

        embed.description = "\n\n".join(lines)
        embed.set_footer(text=f"Requested by <@{int(requested_by)}> • cancel buttons only work for the creator • showing up to {min(limit, 25)}")
        return embed

    async def _send_team_embed(
        self,
        guild_id: int,
        channel_id: int,
        role_id: int,
        *,
        embed: discord.Embed,
        ping_role: bool,
        view: Optional[discord.ui.View] = None,
    ) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        content = None
        allowed = discord.AllowedMentions.none()
        if ping_role:
            role = guild.get_role(role_id)
            content = role.mention if role else f"<@&{role_id}>"
            allowed = discord.AllowedMentions(roles=True, users=False, everyone=False)

        try:
            await channel.send(content=content, embed=embed, view=view, allowed_mentions=allowed)
        except Exception:
            pass

    # ---------- Slash commands ----------
    @guilds_decorator()
    @app_commands.command(
        name="remind",
        description="Schedule a team reminder (includes a 30-minute lead ping)",
    )
    @app_commands.describe(
        when="Time. Format: YYYY-MM-DD HH:MM (America/New_York) or ISO 8601",
        message="Reminder message to post to the team channel",
    )
    async def remind_command(
        self,
        interaction: discord.Interaction,
        when: str,
        message: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            await self._guard(interaction)

            when_local, when_utc = parse_when_to_utc(when, REMIND_TZ)
            if when_utc <= dt.datetime.now(dt.timezone.utc):
                raise RemindError("That time is in the past. Pick a future time.")

            remind_at_utc_ts = int(when_utc.timestamp())
            created_at_utc_ts = _now_utc_ts()

            async with self.lock:
                self.conn.execute(
                    """
                    INSERT INTO reminders (
                        guild_id, channel_id, role_id, created_by,
                        remind_at_utc, message, created_at_utc, pre_sent, main_sent
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
                    """,
                    (
                        interaction.guild.id,
                        TEAM_CHANNEL_ID,
                        TEAM_ROLE_ID,
                        interaction.user.id,
                        remind_at_utc_ts,
                        message,
                        created_at_utc_ts,
                    ),
                )
                self.conn.commit()

            human_local = when_local.strftime("%Y-%m-%d %H:%M %Z")
            lead_note = "Lead reminder fires 30 minutes before (or immediately if inside the window)."
            await interaction.followup.send(f"Scheduled for **{human_local}**.\n{lead_note}", ephemeral=True)

        except RemindError as e:
            await interaction.followup.send(str(e), ephemeral=True)
        except Exception:
            await interaction.followup.send("Unexpected error while scheduling reminder. Check bot logs.", ephemeral=True)

    @guilds_decorator()
    @app_commands.command(
        name="reminders",
        description="Post upcoming team reminders to the team channel (with cancel buttons)",
    )
    @app_commands.describe(
        limit="How many upcoming reminders to show (1-25). Default 10.",
    )
    async def reminders_command(
        self,
        interaction: discord.Interaction,
        limit: Optional[int] = 10,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            await self._guard(interaction)

            lim = 10 if limit is None else int(limit)
            lim = max(1, min(25, lim))

            rows = await self._fetch_upcoming_rows(guild_id=interaction.guild.id, limit=lim)

            embed = self._build_reminders_list_embed(
                rows=rows,
                requested_by=interaction.user.id,
                limit=lim,
            )

            view = RemindersCancelView(
                cog=self,
                guild_id=interaction.guild.id,
                limit=lim,
                rows=rows,
            )

            await self._send_team_embed(
                guild_id=interaction.guild.id,
                channel_id=TEAM_CHANNEL_ID,
                role_id=TEAM_ROLE_ID,
                embed=embed,
                ping_role=False,
                view=view,
            )

            await interaction.followup.send(f"Posted reminders in <#{TEAM_CHANNEL_ID}>.", ephemeral=True)

        except RemindError as e:
            await interaction.followup.send(str(e), ephemeral=True)
        except Exception:
            await interaction.followup.send("Unexpected error while fetching reminders. Check bot logs.", ephemeral=True)

    # ---------- Background poller ----------
    @tasks.loop(seconds=10)
    async def remind_poller(self) -> None:
        if not self.bot.is_ready():
            return

        now_ts = _now_utc_ts()

        # Pass 1: lead reminders
        async with self.lock:
            lead_rows = self.conn.execute(
                """
                SELECT id, guild_id, channel_id, role_id, created_by, remind_at_utc, message
                FROM reminders
                WHERE pre_sent = 0
                  AND main_sent = 0
                  AND (remind_at_utc - ?) <= ?
                ORDER BY remind_at_utc ASC
                LIMIT 25
                """,
                (LEAD_SECONDS, now_ts),
            ).fetchall()

            if lead_rows:
                ids = [r[0] for r in lead_rows]
                self.conn.executemany("UPDATE reminders SET pre_sent = 1 WHERE id = ?", [(i,) for i in ids])
                self.conn.commit()

        for (_id, guild_id, channel_id, role_id, created_by, remind_at_utc, msg) in lead_rows:
            scheduled_ts = int(remind_at_utc)
            embed = self._build_embed(
                title="Team Reminder (30 min lead)",
                message=msg,
                scheduled_ts=scheduled_ts,
                created_by=int(created_by),
                footer_hint="Lead ping. Main reminder will post at the scheduled time.",
            )
            await self._send_team_embed(
                int(guild_id),
                int(channel_id),
                int(role_id),
                embed=embed,
                ping_role=True,
            )

        # Pass 2: main reminders
        async with self.lock:
            main_rows = self.conn.execute(
                """
                SELECT id, guild_id, channel_id, role_id, created_by, remind_at_utc, message
                FROM reminders
                WHERE main_sent = 0
                  AND remind_at_utc <= ?
                ORDER BY remind_at_utc ASC
                LIMIT 25
                """,
                (now_ts,),
            ).fetchall()

            if main_rows:
                ids = [r[0] for r in main_rows]
                self.conn.executemany("DELETE FROM reminders WHERE id = ?", [(i,) for i in ids])
                self.conn.commit()

        for (_id, guild_id, channel_id, role_id, created_by, remind_at_utc, msg) in main_rows:
            scheduled_ts = int(remind_at_utc)
            embed = self._build_embed(
                title="Team Reminder",
                message=msg,
                scheduled_ts=scheduled_ts,
                created_by=int(created_by),
                footer_hint="Main reminder.",
            )
            await self._send_team_embed(
                int(guild_id),
                int(channel_id),
                int(role_id),
                embed=embed,
                ping_role=True,
            )

    @remind_poller.before_loop
    async def _before_remind_poller(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Remind(bot))

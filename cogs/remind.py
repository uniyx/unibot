# cogs/remind.py

import os
import re
import asyncio
import sqlite3
import datetime as dt
from typing import Optional, Tuple, List, Sequence

import discord
from discord import app_commands
from discord.ext import commands, tasks

# =========================
# CONFIG
# =========================

DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None

TEAM_ROLE_ID = int(os.getenv("TEAM_ROLE_ID", "0"))
TEAM_CHANNEL_ID = int(os.getenv("TEAM_CHANNEL_ID", "0"))

REMIND_DB_PATH = os.getenv("REMIND_DB_PATH", "data/reminders.sqlite3")
REMIND_TZ = os.getenv("REMIND_TZ", "America/New_York")
REMIND_POLL_SECONDS = int(os.getenv("REMIND_POLL_SECONDS", "10"))

REMIND_EMBED_COLOR = os.getenv("REMIND_EMBED_COLOR", "#5865F2")

# Daily midnight post of upcoming reminders
REMIND_DAILY_LIMIT = int(os.getenv("REMIND_DAILY_LIMIT", "10"))
# default off; enable explicitly if you really want it
REMIND_DAILY_PING_ROLE = os.getenv("REMIND_DAILY_PING_ROLE", "0").strip() == "1"

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
            main_sent INTEGER NOT NULL DEFAULT 0,

            repeat_kind TEXT,
            repeat_interval INTEGER NOT NULL DEFAULT 1,
            repeat_until_utc INTEGER,
            repeat_remaining INTEGER
        )
        """
    )

    cols = {row[1] for row in conn.execute("PRAGMA table_info(reminders)").fetchall()}

    if "pre_sent" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN pre_sent INTEGER NOT NULL DEFAULT 0")
    if "main_sent" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN main_sent INTEGER NOT NULL DEFAULT 0")

    # recurrence columns (for older DBs)
    if "repeat_kind" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN repeat_kind TEXT")
    if "repeat_interval" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN repeat_interval INTEGER NOT NULL DEFAULT 1")
    if "repeat_until_utc" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN repeat_until_utc INTEGER")
    if "repeat_remaining" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN repeat_remaining INTEGER")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_remind_at ON reminders(remind_at_utc)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pre_sent ON reminders(pre_sent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_main_sent ON reminders(main_sent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_repeat_kind ON reminders(repeat_kind)")
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


def compute_next_occurrence_utc(
    *,
    last_remind_at_utc: int,
    tz: dt.tzinfo,
    repeat_kind: str,
    repeat_interval: int,
) -> int:
    """
    Advance a recurring reminder by whole calendar days/weeks in LOCAL time,
    preserving wall-clock time across DST boundaries.
    """
    last_utc = dt.datetime.fromtimestamp(int(last_remind_at_utc), dt.timezone.utc)
    last_local = last_utc.astimezone(tz)

    hh, mm = last_local.hour, last_local.minute
    interval = max(1, int(repeat_interval))

    if repeat_kind == "daily":
        next_date = last_local.date() + dt.timedelta(days=interval)
        next_local = dt.datetime(next_date.year, next_date.month, next_date.day, hh, mm, tzinfo=tz)
        return int(next_local.astimezone(dt.timezone.utc).timestamp())

    if repeat_kind == "weekly":
        next_date = last_local.date() + dt.timedelta(days=7 * interval)
        next_local = dt.datetime(next_date.year, next_date.month, next_date.day, hh, mm, tzinfo=tz)
        return int(next_local.astimezone(dt.timezone.utc).timestamp())

    raise RemindError(f"Unsupported repeat kind: {repeat_kind}")


# =========================
# UI: Cancel buttons for /reminders
# =========================

class RemindersCancelView(discord.ui.View):
    """
    Non-persistent view (buttons stop working after bot restart).
    Each button cancels one reminder id.
    """

    def __init__(self, cog: "Remind", guild_id: int, limit: int, rows: Sequence[tuple]) -> None:
        super().__init__(timeout=15 * 60)  # 15 minutes
        self.cog = cog
        self.guild_id = guild_id
        self.limit = limit

        # rows: (id, remind_at_utc, message, created_by, pre_sent, main_sent, repeat_kind, repeat_interval, repeat_until_utc, repeat_remaining)
        for idx, r in enumerate(rows[:25], start=1):
            rid = int(r[0])
            created_by = int(r[3])
            self.add_item(ReminderCancelButton(index=idx, reminder_id=rid, created_by=created_by))


class ReminderCancelButton(discord.ui.Button):
    def __init__(self, *, index: int, reminder_id: int, created_by: int) -> None:
        super().__init__(label=f"Cancel {index}", style=discord.ButtonStyle.danger)
        self.reminder_id = reminder_id
        self.created_by = created_by

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = self.view.cog  # type: ignore[attr-defined]

        if interaction.user.id != self.created_by:
            await interaction.response.send_message("You can only cancel reminders that you created.", ephemeral=True)
            return

        try:
            await cog._cancel_reminder_by_id(interaction, self.reminder_id)

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

            if interaction.response.is_done():
                await interaction.followup.send("Cancelled.", ephemeral=True)
            else:
                await interaction.response.send_message("Cancelled.", ephemeral=True)

            try:
                await interaction.message.edit(embed=new_embed, view=new_view)
            except Exception:
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

REPEAT_CHOICES = [
    app_commands.Choice(name="none", value="none"),
    app_commands.Choice(name="daily", value="daily"),
    app_commands.Choice(name="weekly", value="weekly"),
]


class Remind(commands.Cog):
    """
    /remind: schedule a reminder (plus 30-minute lead reminder)
    /reminders: post upcoming reminders to the team channel, with creator-cancel buttons
    Daily at midnight: auto-post upcoming reminders to the team channel
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

        self.daily_reminders_post.start()

    def cog_unload(self) -> None:
        self.remind_poller.cancel()
        self.daily_reminders_post.cancel()
        try:
            self.conn.close()
        except Exception:
            pass

    async def _guard(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            raise RemindError("This command must be used in a server.")
        if not member_has_role(interaction.user, TEAM_ROLE_ID):
            raise RemindError("You do not have permission to use this command.")

    # ---------- DB helpers used by the View ----------
    async def _fetch_upcoming_rows(self, *, guild_id: int, limit: int) -> List[tuple]:
        now_ts = _now_utc_ts()
        async with self.lock:
            return self.conn.execute(
                """
                SELECT id, remind_at_utc, message, created_by, pre_sent, main_sent,
                       repeat_kind, repeat_interval, repeat_until_utc, repeat_remaining
                FROM reminders
                WHERE guild_id = ? AND remind_at_utc > ? AND main_sent = 0
                ORDER BY remind_at_utc ASC
                LIMIT ?
                """,
                (guild_id, now_ts, int(limit)),
            ).fetchall()

    async def _cancel_reminder_by_id(self, interaction: discord.Interaction, reminder_id: int) -> None:
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

            _rid, created_by, _remind_at_utc = row
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

    def _format_repeat_line(
        self,
        *,
        repeat_kind: Optional[str],
        repeat_interval: int,
        repeat_until_utc: Optional[int],
        repeat_remaining: Optional[int],
    ) -> Optional[str]:
        if not repeat_kind:
            return None

        kind = str(repeat_kind)
        interval = max(1, int(repeat_interval or 1))

        bits: List[str] = []
        if kind == "daily":
            bits.append(f"Repeat: daily (every {interval} day{'s' if interval != 1 else ''})")
        elif kind == "weekly":
            bits.append(f"Repeat: weekly (every {interval} week{'s' if interval != 1 else ''})")
        else:
            bits.append(f"Repeat: {kind} (every {interval})")

        if repeat_remaining is not None:
            bits.append(f"remaining: {int(repeat_remaining)}")

        if repeat_until_utc is not None:
            bits.append(f"until: <t:{int(repeat_until_utc)}:d>")

        return " • ".join(bits)

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
        for idx, r in enumerate(rows[:25], start=1):
            _rid, remind_at_utc, msg, created_by, pre_sent, _main_sent, repeat_kind, repeat_interval, repeat_until_utc, repeat_remaining = r

            ts = int(remind_at_utc)
            lead_ts = ts - LEAD_SECONDS
            lead_status = "sent" if int(pre_sent) == 1 else "pending"
            preview = _preview_message(str(msg), max_len=160)

            repeat_line = self._format_repeat_line(
                repeat_kind=repeat_kind if repeat_kind is None else str(repeat_kind),
                repeat_interval=int(repeat_interval or 1),
                repeat_until_utc=int(repeat_until_utc) if repeat_until_utc is not None else None,
                repeat_remaining=int(repeat_remaining) if repeat_remaining is not None else None,
            )

            chunk = (
                f"**{idx}.** <t:{ts}:R> (<t:{ts}:f>)\n"
                f"Lead: <t:{lead_ts}:t> ({lead_status})\n"
                f"By: <@{int(created_by)}>\n"
                f"{preview}"
            )
            if repeat_line:
                chunk += f"\n{repeat_line}"

            lines.append(chunk)

        embed.description = "\n\n".join(lines)
        embed.set_footer(
            text=f"Requested by <@{int(requested_by)}> • cancel buttons only work for the creator • showing up to {min(limit, 25)}"
        )
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
        repeat="Repeat schedule (none/daily/weekly)",
        repeat_interval="Repeat every N days/weeks (default 1)",
        repeat_until="Stop repeating after this time (same formats as when)",
        repeat_count="Stop repeating after N occurrences",
    )
    @app_commands.choices(repeat=REPEAT_CHOICES)
    async def remind_command(
        self,
        interaction: discord.Interaction,
        when: str,
        message: str,
        repeat: Optional[app_commands.Choice[str]] = None,
        repeat_interval: Optional[int] = 1,
        repeat_until: Optional[str] = None,
        repeat_count: Optional[int] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            await self._guard(interaction)

            when_local, when_utc = parse_when_to_utc(when, REMIND_TZ)
            if when_utc <= dt.datetime.now(dt.timezone.utc):
                raise RemindError("That time is in the past. Pick a future time.")

            repeat_value = (repeat.value if repeat else "none")
            rep_kind: Optional[str] = None if repeat_value == "none" else repeat_value
            rep_interval = max(1, int(repeat_interval or 1))

            rep_until_utc_ts: Optional[int] = None
            if repeat_until:
                _u_local, until_utc = parse_when_to_utc(repeat_until, REMIND_TZ)
                rep_until_utc_ts = int(until_utc.timestamp())
                if rep_until_utc_ts <= int(when_utc.timestamp()):
                    raise RemindError("repeat_until must be after the first reminder time.")

            rep_remaining: Optional[int] = None
            if repeat_count is not None:
                rep_remaining = max(1, int(repeat_count))

            remind_at_utc_ts = int(when_utc.timestamp())
            created_at_utc_ts = _now_utc_ts()

            async with self.lock:
                self.conn.execute(
                    """
                    INSERT INTO reminders (
                        guild_id, channel_id, role_id, created_by,
                        remind_at_utc, message, created_at_utc, pre_sent, main_sent,
                        repeat_kind, repeat_interval, repeat_until_utc, repeat_remaining
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)
                    """,
                    (
                        interaction.guild.id,
                        TEAM_CHANNEL_ID,
                        TEAM_ROLE_ID,
                        interaction.user.id,
                        remind_at_utc_ts,
                        message,
                        created_at_utc_ts,
                        rep_kind,
                        rep_interval,
                        rep_until_utc_ts,
                        rep_remaining,
                    ),
                )
                self.conn.commit()

            human_local = when_local.strftime("%Y-%m-%d %H:%M %Z")

            rep_note = ""
            if rep_kind:
                bits = [f"Repeats **{rep_kind}** (every {rep_interval})."]
                if rep_remaining is not None:
                    bits.append(f"Count: **{rep_remaining}**.")
                if rep_until_utc_ts is not None:
                    bits.append(f"Until: <t:{rep_until_utc_ts}:f>.")
                rep_note = "\n" + " ".join(bits)

            lead_note = "Lead reminder fires 30 minutes before (or immediately if inside the window)."
            await interaction.followup.send(f"Scheduled for **{human_local}**.\n{lead_note}{rep_note}", ephemeral=True)

        except RemindError as e:
            await interaction.followup.send(str(e), ephemeral=True)
        except Exception as e:
            # safe, concise error surfaced to you; still ephemeral
            await interaction.followup.send(f"Unexpected error: {type(e).__name__}: {e}", ephemeral=True)
            raise

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

    # =========================
    # Daily midnight post
    # =========================

    @tasks.loop(time=dt.time(hour=0, minute=0, tzinfo=_get_zoneinfo(REMIND_TZ)))
    async def daily_reminders_post(self) -> None:
        if not self.bot.is_ready():
            return

        if len(self.bot.guilds) == 1:
            guild_id = self.bot.guilds[0].id
        else:
            return

        lim = max(1, min(25, int(REMIND_DAILY_LIMIT)))

        try:
            rows = await self._fetch_upcoming_rows(guild_id=int(guild_id), limit=lim)
            embed = self._build_reminders_list_embed(
                rows=rows,
                requested_by=self.bot.user.id if self.bot.user else 0,
                limit=lim,
            )
            view = RemindersCancelView(cog=self, guild_id=int(guild_id), limit=lim, rows=rows)

            # default: no ping (you asked for this)
            await self._send_team_embed(
                guild_id=int(guild_id),
                channel_id=TEAM_CHANNEL_ID,
                role_id=TEAM_ROLE_ID,
                embed=embed,
                ping_role=REMIND_DAILY_PING_ROLE,
                view=view,
            )
        except Exception:
            pass

    @daily_reminders_post.before_loop
    async def _before_daily_reminders_post(self) -> None:
        await self.bot.wait_until_ready()

    # =========================
    # Background poller
    # =========================

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
                self.conn.executemany("UPDATE reminders SET pre_sent = 1 WHERE id = ?", [(int(i),) for i in ids])
                self.conn.commit()

        for (_id, guild_id, channel_id, role_id, created_by, remind_at_utc, msg) in lead_rows:
            scheduled_ts = int(remind_at_utc)
            embed = self._build_embed(
                title="Team Reminder (30 min lead)",
                message=str(msg),
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
                SELECT id, guild_id, channel_id, role_id, created_by, remind_at_utc, message,
                       repeat_kind, repeat_interval, repeat_until_utc, repeat_remaining
                FROM reminders
                WHERE main_sent = 0
                  AND remind_at_utc <= ?
                ORDER BY remind_at_utc ASC
                LIMIT 25
                """,
                (now_ts,),
            ).fetchall()

            to_delete: List[int] = []
            to_update: List[tuple] = []

            for r in main_rows:
                rid = int(r[0])
                remind_at_utc = int(r[5])
                repeat_kind = r[7]  # may be None
                repeat_interval = int(r[8] or 1)
                repeat_until_utc = int(r[9]) if r[9] is not None else None
                repeat_remaining = int(r[10]) if r[10] is not None else None

                if not repeat_kind:
                    to_delete.append(rid)
                    continue

                # decrement remaining occurrences if set
                next_remaining: Optional[int] = None
                if repeat_remaining is not None:
                    next_remaining = repeat_remaining - 1
                    if next_remaining <= 0:
                        to_delete.append(rid)
                        continue
                else:
                    next_remaining = None

                next_ts = compute_next_occurrence_utc(
                    last_remind_at_utc=remind_at_utc,
                    tz=self.tz,
                    repeat_kind=str(repeat_kind),
                    repeat_interval=repeat_interval,
                )

                if repeat_until_utc is not None and next_ts > repeat_until_utc:
                    to_delete.append(rid)
                    continue

                # advance the reminder and reset flags
                to_update.append((next_ts, 0, 0, next_remaining, rid))

            if to_delete:
                self.conn.executemany("DELETE FROM reminders WHERE id = ?", [(i,) for i in to_delete])

            if to_update:
                self.conn.executemany(
                    """
                    UPDATE reminders
                    SET remind_at_utc = ?,
                        pre_sent = ?,
                        main_sent = ?,
                        repeat_remaining = ?
                    WHERE id = ?
                    """,
                    to_update,
                )

            if to_delete or to_update:
                self.conn.commit()

        for (_id, guild_id, channel_id, role_id, created_by, remind_at_utc, msg, _rk, _ri, _ru, _rr) in main_rows:
            scheduled_ts = int(remind_at_utc)
            embed = self._build_embed(
                title="Team Reminder",
                message=str(msg),
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
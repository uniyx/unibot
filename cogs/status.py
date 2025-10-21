# cogs/status.py
import os
import sys
import time
import platform
import asyncio
import datetime as dt
import subprocess
from typing import Optional, Tuple, Dict

import discord
from discord import app_commands
from discord.ext import commands

# Optional deps
try:
    import psutil  # type: ignore
except Exception:
    psutil = None

try:
    import docker  # type: ignore
except Exception:
    docker = None


# ---- helpers ---------------------------------------------------------------

def _fmt_bytes(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    if n >= 100:
        return f"{n:.0f} {units[i]}"
    if n >= 10:
        return f"{n:.1f} {units[i]}"
    return f"{n:.2f} {units[i]}"

def _fmt_pct(p: Optional[float]) -> str:
    return f"{p:.1f}%" if p is not None else "n/a"

def _fmt_dur(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)

def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def _git_meta() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (version_tag, short_sha, full_sha). Prefer env, fall back to git."""
    ver = (os.getenv("UNIBOT_VERSION") or "").strip() or None
    full = (os.getenv("GIT_SHA") or os.getenv("COMMIT_SHA") or "").strip() or None
    short = full[:7] if full else None
    if ver and full:
        return ver, short, full

    def _run(cmd: list[str]) -> Optional[str]:
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=1.5)
            return out.decode().strip()
        except Exception:
            return None

    if not ver:
        ver = _run(["git", "describe", "--tags", "--always", "--dirty"])
    if not full:
        full = _run(["git", "rev-parse", "HEAD"])
        short = full[:7] if full else short
    return ver, short, full

# Guild scoping
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None
def guilds_decorator():
    return app_commands.guilds(discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else (lambda f: f)


# ---- cog -------------------------------------------------------------------

class StatusCog(commands.Cog):
    """Slash /status with compact default and rich verbose output."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot_started = time.monotonic()
        self._last_net_bytes: Optional[Tuple[int, int]] = None
        self._last_net_ts: Optional[float] = None
        self._nic_last: Dict[str, Tuple[int, int, float]] = {}

    @guilds_decorator()
    @app_commands.command(name="status", description="Show service status.")
    @app_commands.describe(verbose="Include deep system details")
    async def status(self, interaction: discord.Interaction, verbose: Optional[bool] = False):
        t0 = time.perf_counter()
        repo = os.getenv("REPO_URL", "https://github.com/uniyx/unibot").rstrip("/")
        embed = discord.Embed(title="unibot Status", color=discord.Color.blurple(), timestamp=_now_utc())
        embed.set_author(name="uniyx/unibot", url=repo)

        # Ping and bot uptime
        ws_ms = int(self.bot.latency * 1000) if getattr(self.bot, "latency", None) else None
        bot_uptime = _fmt_dur(time.monotonic() - self.bot_started)

        # Process stats (compact)
        proc_cpu = None
        rss = None
        if psutil:
            try:
                p = psutil.Process(os.getpid())
                with p.oneshot():
                    p.cpu_percent(None)
                    await asyncio.sleep(0.10)
                    proc_cpu = p.cpu_percent(None)
                    rss = p.memory_info().rss
            except Exception:
                pass

        # Left column: Bot (compact)
        bot_lines = []
        if ws_ms is not None:
            bot_lines.append(f"Ping: `{ws_ms} ms`")
        bot_lines.append(f"Uptime: `{bot_uptime}`")
        if proc_cpu is not None:
            bot_lines.append(f"Proc CPU: `{_fmt_pct(proc_cpu)}`")
        if rss is not None:
            bot_lines.append(f"Proc Mem: `{_fmt_bytes(rss)}`")
        embed.add_field(name="Bot", value="\n".join(bot_lines), inline=True)

        # Right column: Host (compact)
        host_lines = [f"OS: `{platform.system()} {platform.release()}` · Arch: `{platform.machine() or 'unknown'}`"]
        if psutil:
            try:
                boot = psutil.boot_time()
                host_lines.append(f"Host Uptime: `{_fmt_dur(time.time() - boot)}`")
                cpu_logical = psutil.cpu_count(logical=True) or 0
                cpu_phys = psutil.cpu_count(logical=False) or 0
                psutil.cpu_percent(None)
                await asyncio.sleep(0.10)
                cpu_total = psutil.cpu_percent(None)
                host_lines.append(f"CPU: `{cpu_phys}C/{cpu_logical}T` · Util `{_fmt_pct(cpu_total)}`")
                vm = psutil.virtual_memory()
                host_lines.append(f"Mem: `{_fmt_pct(vm.percent)}` ({_fmt_bytes(vm.used)}/{_fmt_bytes(vm.total)})")
            except Exception:
                pass
        else:
            host_lines.append("Install `psutil` for detailed host stats.")
        embed.add_field(name="Host", value="\n".join(host_lines), inline=True)

        # Keep default view minimal: skip disks and network unless verbose
        # Bottom: Versions
        ver, short_sha, full_sha = _git_meta()
        vline = [f"Python `{platform.python_version()}` · discord.py `{discord.__version__}`"]
        if ver:
            vline.append(f"Version `{ver}`")
        if short_sha and full_sha:
            vline.append(f"Commit [`{short_sha}`]({repo}/commit/{full_sha})")
        embed.add_field(name="Versions", value=" · ".join(vline), inline=False)

        # ---- VERBOSE EXTRAS -------------------------------------------------
        if verbose and psutil:
            # New row: CPU detail
            try:
                psutil.cpu_percent(None, percpu=False)
                await asyncio.sleep(0.20)
                per = psutil.cpu_percent(None, percpu=True)
                load_line = None
                if hasattr(os, "getloadavg"):
                    try:
                        l1, l5, l15 = os.getloadavg()
                        threads = psutil.cpu_count(logical=True) or 1
                        load_line = f"Load 1/5/15: `{l1:.2f}/{l5:.2f}/{l15:.2f}` · Norm `{l1/threads:.2f}/{l5/threads:.2f}/{l15/threads:.2f}`"
                    except Exception:
                        pass
                per_core = ", ".join([f"C{i}:{int(v)}%" for i, v in enumerate(per)])
                detail = (load_line + "\n" if load_line else "") + (per_core if per_core else "Per-core n/a")
                embed.add_field(name="CPU Detail", value=detail[:1024], inline=False)
            except Exception:
                pass

            # Next row: Disks
            try:
                parts = psutil.disk_partitions(all=False)
                parts.sort(key=lambda p: p.mountpoint)
                lines = []
                for part in parts:
                    try:
                        u = psutil.disk_usage(part.mountpoint)
                        lines.append(f"`{part.mountpoint}` {_fmt_bytes(u.used)}/{_fmt_bytes(u.total)} · {_fmt_pct(u.percent)}")
                    except Exception:
                        continue
                if lines:
                    embed.add_field(name="Disks", value="\n".join(lines)[:1024], inline=False)
            except Exception:
                pass

            # Next row: Network per-NIC with live rates
            try:
                now = time.monotonic()
                pernic = psutil.net_io_counters(pernic=True)
                nic_lines = []
                for nic, st in sorted(pernic.items()):
                    prev = self._nic_last.get(nic)
                    rxr = txr = "-"
                    if prev:
                        dt_s = max(1e-6, now - prev[2])
                        rxr = _fmt_bytes((st.bytes_recv - prev[0]) / dt_s) + "/s"
                        txr = _fmt_bytes((st.bytes_sent - prev[1]) / dt_s) + "/s"
                    self._nic_last[nic] = (st.bytes_recv, st.bytes_sent, now)
                    nic_lines.append(f"`{nic}` RX {rxr} TX {txr}")
                if nic_lines:
                    embed.add_field(name="Network", value="\n".join(nic_lines)[:1024], inline=False)
            except Exception:
                pass

            # Next row: Top processes
            try:
                snapshots = []
                for p in psutil.process_iter(attrs=["pid", "name"]):
                    try:
                        snapshots.append((p, p.info["name"] or str(p.info["pid"])))
                    except Exception:
                        continue
                await asyncio.sleep(0.15)
                rows = []
                for p, name in snapshots:
                    try:
                        cpu = p.cpu_percent(interval=None)
                        mem = p.memory_info().rss
                        rows.append((cpu, mem, name, p.pid))
                    except Exception:
                        continue
                top_cpu = sorted(rows, key=lambda x: x[0], reverse=True)[:5]
                top_mem = sorted(rows, key=lambda x: x[1], reverse=True)[:5]
                fmt = lambda xs: "\n".join([f"`{n}` PID {pid} · CPU {int(c)}% · RSS {_fmt_bytes(m)}" for c, m, n, pid in xs]) or "n/a"
                embed.add_field(name="Top CPU", value=fmt(top_cpu)[:1024], inline=True)
                embed.add_field(name="Top Memory", value=fmt(top_mem)[:1024], inline=True)
            except Exception:
                pass

            # Next row: Docker brief
            if docker:
                try:
                    client = docker.from_env()
                    containers = client.containers.list(all=True)
                    brief = [f"`{c.name}` [{c.status}]" for c in containers[:12]]
                    if brief:
                        embed.add_field(name="Docker", value="\n".join(brief), inline=False)
                except Exception:
                    pass

        # Footer
        rt_ms = int((time.perf_counter() - t0) * 1000)
        footer = [f"Roundtrip {rt_ms} ms"]
        if ws_ms is not None:
            footer.append(f"Websocket {ws_ms} ms")
        embed.set_footer(text=" | ".join(footer))

        await interaction.response.send_message(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatusCog(bot))

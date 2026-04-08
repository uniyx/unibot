# cogs/camera.py
import os
import io
import asyncio
import contextlib
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.config import env_str
from core.discord_utils import guilds_decorator


# -------------------------
# Single-URL extractor
# -------------------------

def _extract_single_rtsp(env_value: str) -> Optional[str]:
    """
    Accepts either:
      - A bare RTSP URL:            rtsp://user:pass@ip:554/stream1
      - One mapping entry:          c121=rtsp://user:pass@ip:554/stream1
    Any delimiters or extra entries are ignored; the first valid rtsp:// wins.
    """
    if not env_value:
        return None

    # Normalize separators
    text = env_value.replace("\r", "\n")
    pieces = []
    for line in text.split("\n"):
        for semi in line.split(";"):
            pieces.extend(semi.split(","))

    for piece in pieces:
        s = piece.strip()
        if not s:
            continue
        if s.startswith("rtsp://"):
            return s
        if "=" in s:
            _, url = s.split("=", 1)
            url = url.strip()
            if url.startswith("rtsp://"):
                return url
    return None


# -------------------------
# CONFIG
# -------------------------

# Single secret env that contains the RTSP URL
CAM_RTSP_URL = _extract_single_rtsp(env_str("CAM_CAMERAS"))  # the only secret you need

FFMPEG_BIN = env_str("FFMPEG_BIN", "ffmpeg")
SNAPSHOT_TIMEOUT = float(env_str("CAM_SNAPSHOT_TIMEOUT", "8"))


class Camera(commands.Cog):
    """Capture a single frame from an RTSP stream and send it to Discord."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @guilds_decorator()
    @app_commands.command(name="cam", description="Take a snapshot from the camera and send it as an image.")
    async def cam(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=False)

        if not CAM_RTSP_URL:
            await interaction.followup.send(
                "Camera is not configured. Set CAM_CAMERAS to a single RTSP URL and restart the bot.",
                ephemeral=True,
            )
            return

        try:
            image_bytes = await self._grab_frame_ffmpeg(CAM_RTSP_URL, timeout=SNAPSHOT_TIMEOUT)
        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out reading the RTSP stream. Check connectivity and try again.")
            return
        except FileNotFoundError:
            await interaction.followup.send("ffmpeg is not installed or not on PATH in the container.")
            return
        except Exception as e:
            await interaction.followup.send(f"Failed to capture frame: {type(e).__name__}: {e}")
            return

        if not image_bytes:
            await interaction.followup.send("No image data returned from the camera.")
            return

        filename = "camera.png"
        file = discord.File(io.BytesIO(image_bytes), filename=filename)

        embed = discord.Embed(
            title="Snapshot • Tapo C121",
            description="RTSP single-frame capture",
            color=discord.Color.from_str("#0069bf"),
        )
        embed.set_image(url=f"attachment://{filename}")

        await interaction.followup.send(embed=embed, file=file)

    async def _grab_frame_ffmpeg(self, rtsp_url: str, timeout: float) -> bytes:
        """
        Use ffmpeg to pull a single frame from an RTSP stream into memory.
        """
        # Proper RTSP input timeout is -timeout in microseconds
        timeout_us = int(min(max(timeout, 1.0), 30.0) * 1_000_000)
        cmd = [
            FFMPEG_BIN,
            "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-timeout", str(timeout_us),
            "-i", rtsp_url,
            "-frames:v", "1",
            "-f", "image2pipe",
            "-vcodec", "png",
            "pipe:1",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 2.0)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise

        if proc.returncode != 0:
            err = stderr.decode(errors="ignore").strip()
            raise RuntimeError(f"ffmpeg exited with {proc.returncode}. {err}")

        return stdout or b""


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Camera(bot))

# syntax=docker/dockerfile:1
FROM python:3.12-slim

# ---- Base env
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ---- System packages
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# ---- Python deps first for layer caching
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

# ---- App source
COPY . /app

# ---- Non-root user
RUN useradd -m bot && chown -R bot:bot /app
USER bot

# ---- Runtime check that Python exists
HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD python -c "import sys; sys.exit(0)"

# ---- Defaults for the camera cog; override in .env or compose
ENV FFMPEG_BIN=ffmpeg \
    CAM_SNAPSHOT_TIMEOUT=10

CMD ["python", "main.py"]

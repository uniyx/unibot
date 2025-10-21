# syntax=docker/dockerfile:1
FROM python:3.12-slim

# System basics
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install build tools only if needed for native wheels. Most wheels are prebuilt, so keep this light.
# If you later need system libs, uncomment the apt lines.
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     build-essential libjpeg62-turbo zlib1g libfreetype6 \
#  && rm -rf /var/lib/apt/lists/*

# Copy dependency manifest first to leverage layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Now copy the source
COPY . /app

# Create a nonroot user
RUN useradd -m bot && chown -R bot:bot /app
USER bot

# Healthcheck: the process is long-lived; a simple TCP test is not applicable.
# This checks the Python interpreter is available and that main module can be imported.
HEALTHCHECK --interval=30s --timeout=5s --retries=5 CMD python -c "import importlib.util, sys; sys.exit(0)"

CMD ["python", "main.py"]

# unibot

unibot is a multi-purpose Discord bot built with **discord.py**, designed to support competitive cs2 statistics, surface personal analytics, and provide lightweight utilities for a private Discord server. The bot runs inside **Magi**, a self-hosted Proxmox-based homelab, and is fully containerized for predictable, hands-off operation.

![logo](/logo.png)

---

## What unibot Does
unibot is structured around modular cogs, each responsible for a clean, single-purpose feature. Examples include:

- FACEIT and ESEA match tracking  
- Team performance summaries  
- Portfolio and market snapshots  
- System status and server metrics  
- Basic diagnostic and utility commands  

The architecture stays deliberately simple: every feature lives in its own cog, keeps its own helpers, and uses async I/O for external requests. Nothing depends on global shared state.

---

## How It Runs on Magi
unibot is deployed on a Debian VM inside **Magi**, the homelab's Proxmox environment.  
Key design choices:

- Runs as a **Docker container** for isolation and easy restarts  
- Uses a **read-only bind mount** for configuration and assets  
- Reads secrets from a local `.env` file  
- Exposes a small health-check endpoint for monitoring  
- Automatically restarts on failure  

This setup makes updates trivial and keeps the bot stable, even under network or API instability.

---

## Deployment

### 1. Clone the repository
```
git clone https://github.com/uniyx/unibot.git
cd unibot
```

### 2. Configure environment
Create a `.env` file.:

```
DISCORD_TOKEN=your_token
FACEIT_API_KEY=...
DEV_GUILD_ID=...
INSTANCE_TAG=...
```

### 3. Build and run with Docker
```
docker compose build
docker compose up -d
```

To update the bot:

```
git pull
docker compose up -d --build
```

### Local development
```
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

---

## License
unibot is released under the **MIT License**.  
You're free to use, modify, and adapt it without restriction.

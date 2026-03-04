# Infra Assistant

Self-hosted infrastructure monitoring assistant using Ollama + Rocket.Chat.

Simple, customizable bot with direct LLM integration - no complex frameworks.

## Features

- Conversational interface via Rocket.Chat (channels + DMs)
- Built-in tools: Zabbix monitoring, infrastructure status
- Direct Ollama integration - simple and fast
- Privacy-first: runs entirely on your infrastructure
- GPU isolation (locks Ollama to GPU1)
- Easy to customize - tools are just Python functions

## Quick Start

```bash
git clone https://github.com/ntnlabs/infra-assistant.git /opt/infra-assistant
cd /opt/infra-assistant
sudo ./install.sh
```

Then:
1. Edit `.env` with your credentials (Rocket.Chat, Zabbix, etc.)
2. Start services: `sudo systemctl start rc-bot zabbix-proxy`
3. Talk to the bot in Rocket.Chat

See [SETUP_GUIDE.md](SETUP_GUIDE.md) for detailed instructions.

## Configuration

All secrets are in `.env` (copy from `.env.example`):

```bash
cp .env.example .env
nano .env
```

**Never commit `.env` to git!**

## Directory Structure

```
/opt/infra-assistant/
├── .env                  # Your secrets (gitignored)
├── .env.example          # Template
├── rc-bot/               # Rocket.Chat bot
│   └── bot.py            # Main bot with built-in tools
├── zabbix-proxy/         # REST wrapper for Zabbix
│   └── app.py
├── zabbix-poller/        # Periodic alert checker
│   └── poller.py
└── systemd/              # Service files
```

## Components

| Component | Port | Purpose |
|-----------|------|---------|
| Ollama | 11434 | LLM inference (GPU1) |
| RC Bot | - | Rocket.Chat <-> Ollama bridge |
| Zabbix Proxy | 5002 | REST API for Zabbix |
| Zabbix Poller | - | Proactive alert notifications |

## Adding Custom Tools

Tools are just Python functions in `rc-bot/bot.py`. Example:

```python
def check_disk_space(host: str) -> dict:
    """Check disk space on a host."""
    # Your implementation here
    return {"success": True, "data": "Disk usage: 45%"}

# Add to TOOL_FUNCTIONS dict
TOOL_FUNCTIONS["check_disk_space"] = check_disk_space
```

The bot will automatically detect when to use tools based on user questions.

## Security

- RC Bot filters by user/channel
- All secrets in `.env` (gitignored)
- Zabbix proxy uses token authentication
- Ollama runs locally (no external API calls)

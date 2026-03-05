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
# Clone to your preferred location
git clone https://github.com/ntnlabs/infra-assistant.git
cd infra-assistant

# Edit configuration
cp .env.example .env
nano .env

# Set up systemd services (see systemd/README.md)
```

See [OPERATIONS.md](OPERATIONS.md) for detailed operations guide.

## Configuration

All secrets are in `.env` (copy from `.env.example`):

```bash
cp .env.example .env
nano .env
```

**Never commit `.env` to git!**

## Directory Structure

```
infra-assistant/
├── .env                  # Your secrets (gitignored)
├── .env.example          # Template
├── rc-bot/               # Rocket.Chat bot
│   └── bot.py            # Main bot with built-in tools
├── ssh-proxy/            # SSH command validator (independent)
│   ├── app.py
│   ├── hosts.yaml        # Allowed hosts (gitignored)
│   └── commands.yaml     # Allowed commands
├── zabbix-proxy/         # REST wrapper for Zabbix
│   └── app.py
├── zabbix-poller/        # Periodic alert checker
│   └── poller.py
└── systemd/              # Service files
```

## Components

| Component | Port | Purpose |
|-----------|------|---------|
| Ollama | 11434 | LLM inference (GPU) |
| RC Bot | - | Rocket.Chat <-> Ollama bridge |
| SSH Proxy | 5001 | Independent command validator |
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
- SSH commands validated by independent SSH Proxy (Bot cannot bypass)
- Command and host allowlists managed separately from Bot
- All secrets in `.env` (gitignored)
- Zabbix proxy uses token authentication
- Ollama runs locally (no external API calls)

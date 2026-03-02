# Infra Assistant

Self-hosted infrastructure monitoring assistant using Dify + Ollama + Rocket.Chat.

## Features

- Conversational interface via Rocket.Chat
- Reads Zabbix and Graylog APIs
- Executes allowed SSH diagnostic commands
- Privacy-first: runs entirely on your infrastructure
- GPU isolation (locks Ollama to GPU1)

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/infra-assistant.git /opt/infra-assistant
cd /opt/infra-assistant
sudo ./install.sh
```

Then:
1. Open `http://YOUR_SERVER_IP/install` - complete Dify setup
2. Edit `.env` with your credentials
3. Edit `ssh-proxy/hosts.yaml` with your hosts
4. Start services

See [SETUP_GUIDE.md](../SETUP_GUIDE.md) for detailed instructions.

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
├── dify/                 # Dify (auto-cloned)
├── ssh-proxy/
│   ├── hosts.yaml        # Your hosts (gitignored)
│   ├── hosts.yaml.example
│   ├── commands.yaml     # Allowed commands
│   └── ...
├── rc-bot/
│   └── ...
└── keys/                 # SSH keys (gitignored)
```

## Components

| Component | Port | Purpose |
|-----------|------|---------|
| Dify | 80 | Agent/workflow platform |
| Ollama | 11434 | LLM inference (GPU1) |
| SSH Proxy | 5001 | Controlled SSH execution |
| RC Bot | - | Rocket.Chat bridge |

## Security

- SSH Proxy uses command allowlist
- RC Bot filters by user/channel
- All secrets in `.env` (gitignored)
- Host configs in `hosts.yaml` (gitignored)

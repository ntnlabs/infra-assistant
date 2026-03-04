# Operations Guide

Day-to-day operations and customization for the Infrastructure Assistant.

## Table of Contents

- [Customizing the Bot](#customizing-the-bot)
  - [Tuning the System Prompt](#tuning-the-system-prompt)
  - [Adding New Tools/Functions](#adding-new-toolsfunctions)
  - [Adjusting Model Parameters](#adjusting-model-parameters)
- [Managing Services](#managing-services)
  - [Starting/Stopping Services](#startingstopping-services)
  - [Disabling the Zabbix Poller](#disabling-the-zabbix-poller)
  - [Viewing Logs](#viewing-logs)
- [Troubleshooting](#troubleshooting)

---

## Customizing the Bot

### Tuning the System Prompt

The system prompt controls how the bot behaves. Edit `rc-bot/bot.py` around line 80:

```python
SYSTEM_PROMPT = """You are an infrastructure assistant helping with monitoring and operations.

You have access to tools to check system status and alerts. When users ask about infrastructure,
use the appropriate tools to get real-time information.

Be concise and helpful. Focus on actionable information."""
```

**Tips for writing good prompts:**
- Be specific about the bot's role and capabilities
- Tell it what tools it has access to
- Set the tone (formal/casual, verbose/concise)
- Add constraints if needed (e.g., "Always ask before running destructive commands")

**Example - More detailed prompt:**
```python
SYSTEM_PROMPT = """You are Bob, the infrastructure monitoring assistant for the operations team.

Your role:
- Monitor Zabbix alerts and provide analysis
- Answer questions about infrastructure status
- Recommend troubleshooting steps

Available tools:
- get_active_alerts: Check current Zabbix problems
- get_infrastructure_summary: Get overview of hosts and status

Communication style:
- Be concise and technical
- Use bullet points for lists
- Highlight critical issues with severity levels
- Always provide actionable next steps"""
```

**After editing:**
```bash
sudo systemctl restart rc-bot
```

### Adding New Tools/Functions

Tools are Python functions in `rc-bot/bot.py`. Here's how to add one:

#### Step 1: Write the Function

Add your function to the "Tools" section (around line 95):

```python
def check_service_status(service_name: str) -> dict:
    """Check if a systemd service is running.

    Args:
        service_name: Name of the service (e.g., 'nginx', 'postgres')

    Returns:
        dict with 'success' and 'data' or 'error'
    """
    try:
        import subprocess
        result = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True,
            text=True,
            timeout=5
        )

        status = result.stdout.strip()
        if status == "active":
            return {"success": True, "data": f"✅ {service_name} is running"}
        else:
            return {"success": True, "data": f"❌ {service_name} is {status}"}

    except Exception as e:
        logger.error(f"Error checking service: {e}")
        return {"success": False, "error": str(e)}
```

#### Step 2: Add Tool Definition

Add to the `TOOLS` list (around line 165):

```python
TOOLS = [
    {
        "name": "get_active_alerts",
        "description": "Get current active alerts from Zabbix monitoring system. Use this when users ask about problems, alerts, or issues.",
        "parameters": {
            "min_severity": {
                "type": "integer",
                "description": "Minimum severity level (0-5). Default: 3",
                "default": 3
            }
        }
    },
    # ... existing tools ...
    {
        "name": "check_service_status",
        "description": "Check if a systemd service is running. Use when users ask about service status.",
        "parameters": {
            "service_name": {
                "type": "string",
                "description": "Name of the service (e.g., 'nginx', 'postgresql', 'docker')"
            }
        }
    }
]
```

#### Step 3: Register the Function

Add to `TOOL_FUNCTIONS` dict (around line 185):

```python
TOOL_FUNCTIONS = {
    "get_active_alerts": get_active_alerts,
    "get_infrastructure_summary": get_infrastructure_summary,
    "check_service_status": check_service_status  # Add this line
}
```

#### Step 4: Add Detection Logic

In the `call_ollama` method (around line 340), add detection:

```python
if "check_service_status" in content.lower() or "service status" in content.lower():
    if not tool_called and iteration == 1:
        # Extract service name from user's message
        import re
        service_match = re.search(r'(nginx|postgres|docker|redis|zabbix|ollama)', text.lower())
        if service_match:
            service_name = service_match.group(1)
            logger.info(f"Checking service status: {service_name}")
            tool_result = check_service_status(service_name)
            if tool_result["success"]:
                messages.append({"role": "assistant", "content": f"Let me check {service_name}."})
                messages.append({"role": "user", "content": f"Service status:\n{tool_result['data']}\n\nPlease respond to the original question."})
                tool_called = True
```

#### Step 5: Test and Deploy

```bash
# Test the syntax
cd /opt/infra-assistant/rc-bot
python3 -c "import bot"

# If no errors, restart
sudo systemctl restart rc-bot

# Check logs
sudo journalctl -u rc-bot -f
```

**Example conversation after adding the tool:**
```
User: Is nginx running?
Bot: ✅ nginx is running
```

### Adjusting Model Parameters

Edit `rc-bot/bot.py` around line 315 in the `call_ollama` method:

```python
"options": {
    "temperature": 0.7,      # Lower = more focused, Higher = more creative (0.0-1.0)
    "num_ctx": 4096,         # Context window size (tokens)
    "top_p": 0.9,            # Nucleus sampling (optional)
    "top_k": 40,             # Top-k sampling (optional)
    "repeat_penalty": 1.1    # Penalize repetition (optional)
}
```

**Common adjustments:**
- **Temperature 0.3-0.5**: For factual, technical responses
- **Temperature 0.7-0.9**: For more conversational responses
- **num_ctx 2048**: Smaller context for faster responses
- **num_ctx 8192**: Larger context for complex conversations (uses more GPU memory)

---

## Managing Services

### Starting/Stopping Services

**RC Bot:**
```bash
# Start
sudo systemctl start rc-bot

# Stop
sudo systemctl stop rc-bot

# Restart
sudo systemctl restart rc-bot

# Status
sudo systemctl status rc-bot

# Disable auto-start
sudo systemctl disable rc-bot

# Enable auto-start
sudo systemctl enable rc-bot
```

**Zabbix Proxy:**
```bash
sudo systemctl start zabbix-proxy
sudo systemctl stop zabbix-proxy
sudo systemctl restart zabbix-proxy
```

**Ollama:**
```bash
sudo systemctl start ollama
sudo systemctl stop ollama
sudo systemctl restart ollama
```

### Disabling the Zabbix Poller

The Zabbix poller runs every 5 minutes via systemd timer and posts new alerts to Rocket.Chat.

**To stop it completely:**
```bash
# Stop the timer
sudo systemctl stop zabbix-poller.timer

# Disable it (won't start on boot)
sudo systemctl disable zabbix-poller.timer

# Check status
sudo systemctl status zabbix-poller.timer
```

**To temporarily pause it:**
```bash
# Stop the timer (but keep it enabled for next boot)
sudo systemctl stop zabbix-poller.timer

# Start it again later
sudo systemctl start zabbix-poller.timer
```

**To change the polling interval:**

Edit `/opt/infra-assistant/systemd/zabbix-poller.timer`:
```ini
[Timer]
OnBootSec=1min
OnUnitActiveSec=15min    # Change from 5min to 15min
AccuracySec=30s
```

Then reload:
```bash
sudo systemctl daemon-reload
sudo systemctl restart zabbix-poller.timer
```

**To change alert severity threshold:**

Edit `.env`:
```bash
ALERT_MIN_SEVERITY=4    # Only High and Disaster (was 3 for Average+)
```

Restart is not needed - next run will use new value.

### Viewing Logs

**Live logs (follow mode):**
```bash
# RC Bot
sudo journalctl -u rc-bot -f

# Zabbix Proxy
sudo journalctl -u zabbix-proxy -f

# Ollama
sudo journalctl -u ollama -f

# Zabbix Poller (shows last run)
sudo journalctl -u zabbix-poller -n 50
```

**Recent logs:**
```bash
# Last 100 lines
sudo journalctl -u rc-bot -n 100

# Last hour
sudo journalctl -u rc-bot --since "1 hour ago"

# Today only
sudo journalctl -u rc-bot --since today
```

**All services at once:**
```bash
sudo journalctl -u rc-bot -u zabbix-proxy -u ollama -f
```

---

## Troubleshooting

### Bot Not Responding in Rocket.Chat

1. **Check if bot is running:**
   ```bash
   sudo systemctl status rc-bot
   ```

2. **Check logs for errors:**
   ```bash
   sudo journalctl -u rc-bot -n 50
   ```

3. **Verify Ollama is running:**
   ```bash
   sudo systemctl status ollama
   curl http://localhost:11434/api/tags
   ```

4. **Test Ollama manually:**
   ```bash
   ollama run llama3.1:8b
   # Type a test message
   ```

5. **Check RC credentials in `.env`:**
   ```bash
   grep RC_ /opt/infra-assistant/.env
   ```

### Tools Not Working

1. **Check Zabbix proxy is running:**
   ```bash
   sudo systemctl status zabbix-proxy
   curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:5002/summary
   ```

2. **Verify ZABBIX_PROXY_TOKEN in `.env`:**
   ```bash
   grep ZABBIX /opt/infra-assistant/.env
   ```

3. **Check bot logs for tool errors:**
   ```bash
   sudo journalctl -u rc-bot -f | grep -i error
   ```

### Bot Responses are Slow

1. **Check Ollama GPU usage:**
   ```bash
   nvidia-smi
   # Should show llama process on GPU1
   ```

2. **Reduce context window in bot.py:**
   ```python
   "num_ctx": 2048  # Instead of 4096
   ```

3. **Check if GPU1 is locked correctly:**
   ```bash
   sudo systemctl cat ollama | grep CUDA
   # Should show: Environment="CUDA_VISIBLE_DEVICES=1"
   ```

### Conversation History Issues

Conversations expire after 1 hour by default. To change:

Edit `.env`:
```bash
CONVERSATION_TIMEOUT=7200  # 2 hours in seconds
```

Restart bot:
```bash
sudo systemctl restart rc-bot
```

### Out of GPU Memory

If you see CUDA out of memory errors:

1. **Use a smaller model:**
   ```bash
   ollama pull llama3.1:7b  # Smaller than 8b
   ```

   Update `.env`:
   ```bash
   OLLAMA_MODEL=llama3.1:7b
   ```

2. **Reduce context window:**
   Edit `rc-bot/bot.py`:
   ```python
   "num_ctx": 2048  # Smaller context
   ```

3. **Restart Ollama to clear memory:**
   ```bash
   sudo systemctl restart ollama
   ```

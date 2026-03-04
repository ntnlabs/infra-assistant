#!/usr/bin/env python3
"""
Rocket.Chat <-> Ollama Bot
==========================
Infrastructure assistant bot with direct Ollama integration.

Configuration via environment variables (or .env file):
- RC_URL, RC_USERNAME, RC_PASSWORD
- RC_CHANNELS, RC_ALLOWED_USERS, RC_PREFIX
- OLLAMA_URL, OLLAMA_MODEL
- ZABBIX_PROXY_URL, ZABBIX_PROXY_TOKEN
- POLL_INTERVAL, CONVERSATION_TIMEOUT, DEBUG

Usage:
    python bot.py
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from rocketchat_API.rocketchat import RocketChat

# Try to load .env file
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass

# =============================================================================
# Configuration from Environment
# =============================================================================

# Rocket.Chat
RC_URL = os.environ.get("RC_URL", "")
RC_USERNAME = os.environ.get("RC_USERNAME", "")
RC_PASSWORD = os.environ.get("RC_PASSWORD", "")
RC_CHANNELS = [c.strip() for c in os.environ.get("RC_CHANNELS", "").split(",") if c.strip()]
RC_ALLOWED_USERS = [u.strip() for u in os.environ.get("RC_ALLOWED_USERS", "").split(",") if u.strip()]
RC_PREFIX = os.environ.get("RC_PREFIX", "").strip()

# Ollama
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")

# Zabbix (for tools)
ZABBIX_PROXY_URL = os.environ.get("ZABBIX_PROXY_URL", "http://localhost:5002")
ZABBIX_PROXY_TOKEN = os.environ.get("ZABBIX_PROXY_TOKEN", "")

# Settings
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "2"))
CONVERSATION_TIMEOUT = int(os.environ.get("CONVERSATION_TIMEOUT", "3600"))
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

# =============================================================================
# Logging Setup
# =============================================================================

log_level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# =============================================================================
# State
# =============================================================================

# Conversation tracking: "room_id:user" -> {"messages": list, "last_activity": datetime}
conversations: dict = {}

# Track processed message IDs to avoid duplicates
processed_messages: set = set()
MAX_PROCESSED_MESSAGES = 10000

# System prompt for the bot
SYSTEM_PROMPT = """You are Bob, an infrastructure monitoring and operations assistant for the IT operations team.

## Your Role and Responsibilities

You help the operations team by:
- Monitoring infrastructure health through Zabbix integration
- Analyzing alerts and identifying patterns or correlations
- Providing clear, actionable recommendations for issue resolution
- Answering questions about system status and metrics
- Assisting with troubleshooting by gathering relevant information

## Available Tools

You have access to these tools - use them proactively when relevant:

1. **get_active_alerts** - Retrieves current problems from Zabbix monitoring
   - Use when: Users ask about alerts, problems, issues, or "what's wrong"
   - Returns: List of active alerts with severity and affected hosts
   - Can filter by severity level (0-5)

2. **get_infrastructure_summary** - Gets overview of monitored infrastructure
   - Use when: Users ask about overall status, host counts, or general health
   - Returns: Total hosts, hosts up/down, active problems, and trigger counts

## Communication Guidelines

**Tone and Style:**
- Be professional but conversational
- Use clear, technical language appropriate for operations teams
- Be concise - operations teams need quick answers
- Use bullet points and structure for readability

**When Analyzing Alerts:**
- Group related alerts together (e.g., multiple alerts from same host)
- Highlight severity levels clearly (use terms like "Critical", "High", "Average")
- Identify patterns (e.g., "All web servers showing high load")
- Suggest logical next steps for investigation
- Mention if alerts might be related (network issues affecting multiple hosts, etc.)

**When Providing Recommendations:**
- Be specific and actionable
- Prioritize by severity and impact
- Consider dependencies (e.g., database down affects application)
- Suggest verification steps before taking action
- Note if an issue requires escalation

**Response Format:**
- For single questions: Direct answer with relevant details
- For multiple alerts: Organized by severity or affected system
- For status requests: Summary first, details if needed
- Always end with "Let me know if you need more details" or similar

## Important Behaviors

- **Always check tools first** before saying you don't know about current status
- **Be honest** if you don't have access to information - don't make up data
- **Ask clarifying questions** if a request is ambiguous
- **Acknowledge limitations** - you can monitor and recommend, but humans make final decisions
- **Maintain context** - remember what was discussed earlier in the conversation
- **Be proactive** - if you see critical alerts, mention them even if not directly asked

## Example Interactions

User: "What's going on with the infrastructure?"
You: Use get_infrastructure_summary, then get_active_alerts if there are problems, provide overview

User: "Show me critical alerts"
You: Use get_active_alerts with appropriate severity filter, format clearly with severity indicators

User: "Why is the website slow?"
You: Check alerts for web servers, load balancers, databases - look for patterns and suggest causes

Remember: Your goal is to help the operations team work efficiently by providing accurate, timely information and intelligent analysis."""

# =============================================================================
# Tools
# =============================================================================

def get_active_alerts(min_severity: int = 3, limit: int = 25) -> dict:
    """Get active alerts from Zabbix.

    Args:
        min_severity: Minimum severity (0-5). Default 3 (Average).
        limit: Max number of alerts to return.

    Returns:
        dict with 'success' and 'data' or 'error'
    """
    try:
        response = requests.get(
            f"{ZABBIX_PROXY_URL}/problems",
            headers={"Authorization": f"Bearer {ZABBIX_PROXY_TOKEN}"},
            params={"severity": min_severity, "limit": limit},
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        problems = data.get("problems", [])

        if not problems:
            return {"success": True, "data": "No active alerts found."}

        # Format for readability
        result = f"Found {len(problems)} active alerts:\n\n"
        for p in problems[:limit]:
            severity = p.get("severity", 0)
            severity_names = ["Not classified", "Info", "Warning", "Average", "High", "Disaster"]
            sev_name = severity_names[severity] if 0 <= severity <= 5 else "Unknown"
            result += f"[{sev_name}] {p.get('name', 'Unknown')} on {p.get('hostname', 'Unknown')}\n"

        return {"success": True, "data": result}

    except Exception as e:
        logger.error(f"Error getting alerts: {e}")
        return {"success": False, "error": str(e)}


def get_infrastructure_summary() -> dict:
    """Get infrastructure overview from Zabbix.

    Returns:
        dict with 'success' and 'data' or 'error'
    """
    try:
        response = requests.get(
            f"{ZABBIX_PROXY_URL}/summary",
            headers={"Authorization": f"Bearer {ZABBIX_PROXY_TOKEN}"},
            timeout=30
        )
        response.raise_for_status()
        data = response.json()

        summary = data.get("summary", {})
        result = f"""Infrastructure Summary:
- Total Hosts: {summary.get('total_hosts', 0)}
- Hosts Up: {summary.get('hosts_up', 0)}
- Hosts Down: {summary.get('hosts_down', 0)}
- Active Problems: {summary.get('active_problems', 0)}
- Active Triggers: {summary.get('active_triggers', 0)}"""

        return {"success": True, "data": result}

    except Exception as e:
        logger.error(f"Error getting summary: {e}")
        return {"success": False, "error": str(e)}


# Tool definitions for LLM
TOOLS = [
    {
        "name": "get_active_alerts",
        "description": "Get current active alerts from Zabbix monitoring system. Use this when users ask about problems, alerts, or issues.",
        "parameters": {
            "min_severity": {
                "type": "integer",
                "description": "Minimum severity level (0=Not classified, 1=Info, 2=Warning, 3=Average, 4=High, 5=Disaster). Default: 3",
                "default": 3
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of alerts to return. Default: 25",
                "default": 25
            }
        }
    },
    {
        "name": "get_infrastructure_summary",
        "description": "Get overview of infrastructure status including host counts and active problems. Use this for general status questions.",
        "parameters": {}
    }
]

# Map tool names to functions
TOOL_FUNCTIONS = {
    "get_active_alerts": get_active_alerts,
    "get_infrastructure_summary": get_infrastructure_summary
}


# =============================================================================
# Bot Class
# =============================================================================

class RocketChatBot:
    """Rocket.Chat bot that forwards messages to Dify."""

    def __init__(self):
        self.rc: Optional[RocketChat] = None
        self.room_ids: dict = {}  # channel_name -> room_id
        self.dm_room_ids: set = set()  # DM room IDs

    def connect(self) -> bool:
        """Connect to Rocket.Chat server."""
        try:
            self.rc = RocketChat(
                user=RC_USERNAME,
                password=RC_PASSWORD,
                server_url=RC_URL
            )
            logger.info(f"Connected to Rocket.Chat at {RC_URL}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Rocket.Chat: {e}")
            return False

    def get_room_id(self, channel_name: str) -> Optional[str]:
        """Get room ID for a channel name."""
        # Try public channel
        try:
            result = self.rc.channels_info(channel=channel_name)
            if result.ok:
                return result.json().get("channel", {}).get("_id")
        except Exception:
            pass

        # Try private group
        try:
            result = self.rc.groups_info(room_name=channel_name)
            if result.ok:
                return result.json().get("group", {}).get("_id")
        except Exception:
            pass

        return None

    def setup_channels(self) -> bool:
        """Get room IDs for configured channels."""
        for channel in RC_CHANNELS:
            room_id = self.get_room_id(channel)
            if room_id:
                self.room_ids[channel] = room_id
                logger.info(f"Monitoring channel: {channel} (ID: {room_id})")
            else:
                logger.warning(f"Could not find channel: {channel}")

        if not self.room_ids:
            logger.error("No valid channels found!")
            return False

        return True

    def setup_dms(self):
        """Get DM room IDs."""
        try:
            # Get list of DM rooms
            result = self.rc.im_list()
            if result.ok:
                ims = result.json().get("ims", [])
                for im in ims:
                    room_id = im.get("_id")
                    username = im.get("username", "unknown")
                    if room_id:
                        self.dm_room_ids.add(room_id)
                        logger.info(f"Monitoring DM with: {username}")

                if self.dm_room_ids:
                    logger.info(f"Monitoring {len(self.dm_room_ids)} DM conversations")
        except Exception as e:
            logger.warning(f"Could not setup DMs: {e}")

    def should_respond(self, message: dict, is_dm: bool = False) -> bool:
        """Check if bot should respond to this message."""
        msg_id = message.get("_id", "")

        # Skip already processed
        if msg_id in processed_messages:
            return False

        # Skip own messages
        sender = message.get("u", {}).get("username", "")
        if sender == RC_USERNAME:
            return False

        # Skip bot messages
        if message.get("bot"):
            return False

        # Check user allowlist (if configured)
        if RC_ALLOWED_USERS:
            if sender not in RC_ALLOWED_USERS:
                logger.debug(f"User {sender} not in allowed list")
                return False

        # Check prefix (only for channels, not DMs)
        if not is_dm and RC_PREFIX:
            text = message.get("msg", "")
            if not text.lower().startswith(RC_PREFIX.lower()):
                return False

        return True

    def get_message_text(self, message: dict, is_dm: bool = False) -> str:
        """Extract and clean message text."""
        text = message.get("msg", "")

        # Remove prefix if configured (only for channels, not DMs)
        if not is_dm and RC_PREFIX and text.lower().startswith(RC_PREFIX.lower()):
            text = text[len(RC_PREFIX):].strip()

        return text

    def get_conversation_history(self, room_id: str, user: str) -> list:
        """Get conversation history for a room + user."""
        conv_key = f"{room_id}:{user}"

        if conv_key in conversations:
            conv = conversations[conv_key]

            # Check if conversation is still active
            if CONVERSATION_TIMEOUT > 0:
                elapsed = datetime.now() - conv["last_activity"]
                if elapsed > timedelta(seconds=CONVERSATION_TIMEOUT):
                    logger.info(f"Conversation expired for {user} in room {room_id}")
                    del conversations[conv_key]
                    return []

            return conv.get("messages", [])

        return []

    def update_conversation(self, room_id: str, user: str, user_msg: str, assistant_msg: str):
        """Update conversation history."""
        conv_key = f"{room_id}:{user}"

        if conv_key not in conversations:
            conversations[conv_key] = {
                "messages": [],
                "last_activity": datetime.now()
            }

        # Add messages to history
        conversations[conv_key]["messages"].append({"role": "user", "content": user_msg})
        conversations[conv_key]["messages"].append({"role": "assistant", "content": assistant_msg})
        conversations[conv_key]["last_activity"] = datetime.now()

        # Keep only last 20 messages (10 exchanges)
        if len(conversations[conv_key]["messages"]) > 20:
            conversations[conv_key]["messages"] = conversations[conv_key]["messages"][-20:]

    def call_ollama(self, text: str, room_id: str, user: str) -> str:
        """Send message to Ollama and get response with tool support."""
        import json as json_lib

        # Get conversation history
        history = self.get_conversation_history(room_id, user)

        # Build messages array
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(history)
        messages.append({"role": "user", "content": text})

        max_iterations = 5  # Prevent infinite loops
        iteration = 0

        try:
            while iteration < max_iterations:
                iteration += 1

                # Call Ollama
                response = requests.post(
                    f"{OLLAMA_URL}/api/chat",
                    json={
                        "model": OLLAMA_MODEL,
                        "messages": messages,
                        "stream": False,
                        "options": {
                            "temperature": 0.7,
                            "num_ctx": 4096
                        }
                    },
                    timeout=120
                )

                if response.status_code != 200:
                    logger.error(f"Ollama returned {response.status_code}: {response.text}")
                    return f"Error: Ollama API returned {response.status_code}"

                response.raise_for_status()
                data = response.json()
                assistant_message = data.get("message", {})
                content = assistant_message.get("content", "").strip()

                # Check if response contains a tool call
                # Simple pattern: if message mentions using a tool, try to extract it
                tool_called = False

                # Look for simple tool call pattern in response
                if "get_active_alerts" in content.lower() or "active alerts" in content.lower() or "problems" in content.lower():
                    if not tool_called and iteration == 1:  # Only on first iteration
                        logger.info("Detected need for active alerts")
                        tool_result = get_active_alerts()
                        if tool_result["success"]:
                            # Add tool result to messages and retry
                            messages.append({"role": "assistant", "content": "Let me check the active alerts."})
                            messages.append({"role": "user", "content": f"Here are the active alerts:\n{tool_result['data']}\n\nPlease analyze and respond to the original question."})
                            tool_called = True

                if "get_infrastructure_summary" in content.lower() or "infrastructure status" in content.lower() or "summary" in content.lower():
                    if not tool_called and iteration == 1:
                        logger.info("Detected need for infrastructure summary")
                        tool_result = get_infrastructure_summary()
                        if tool_result["success"]:
                            messages.append({"role": "assistant", "content": "Let me check the infrastructure summary."})
                            messages.append({"role": "user", "content": f"Here is the infrastructure summary:\n{tool_result['data']}\n\nPlease analyze and respond to the original question."})
                            tool_called = True

                # If no tool was called, we have our final answer
                if not tool_called:
                    # Update conversation history
                    self.update_conversation(room_id, user, text, content)
                    return content or "No response from assistant."

                # Otherwise, continue loop with tool results

            return "Error: Too many tool iterations"

        except requests.exceptions.Timeout:
            logger.error("Ollama request timed out")
            return "Request timed out. The assistant is taking too long."

        except requests.exceptions.ConnectionError:
            logger.error("Cannot connect to Ollama")
            return "Error: Cannot connect to Ollama service."

        except Exception as e:
            logger.error(f"Ollama API error: {e}")
            return f"Error: {str(e)}"

    def send_message(self, room_id: str, text: str):
        """Send message to Rocket.Chat room."""
        try:
            result = self.rc.chat_post_message(text, room_id=room_id)
            if not result.ok:
                logger.error(f"Failed to send message: {result.json()}")
        except Exception as e:
            logger.error(f"Error sending message: {e}")

    def process_message(self, message: dict, room_id: str, is_dm: bool = False):
        """Process a single message."""
        msg_id = message.get("_id", "")

        # Mark as processed
        processed_messages.add(msg_id)

        # Cleanup old processed messages
        if len(processed_messages) > MAX_PROCESSED_MESSAGES:
            to_remove = list(processed_messages)[:MAX_PROCESSED_MESSAGES // 2]
            for item in to_remove:
                processed_messages.discard(item)

        # Extract info
        text = self.get_message_text(message, is_dm=is_dm)
        user = message.get("u", {}).get("username", "unknown")

        if not text:
            return

        msg_type = "DM" if is_dm else "channel"
        logger.info(f"[{msg_type}] Message from {user}: {text[:100]}...")

        # Get response from Ollama
        response = self.call_ollama(text, room_id, user)

        # Send response
        self.send_message(room_id, response)
        logger.info(f"Sent response ({len(response)} chars)")

    def poll_messages(self):
        """Poll for new messages in all monitored rooms."""
        for channel_name, room_id in self.room_ids.items():
            try:
                # Try public channel
                result = self.rc.channels_history(room_id=room_id, count=10)

                # If that fails, try private group
                if not result.ok:
                    result = self.rc.groups_history(room_id=room_id, count=10)

                if result.ok:
                    messages = result.json().get("messages", [])

                    # Process oldest first
                    for message in reversed(messages):
                        if self.should_respond(message, is_dm=False):
                            self.process_message(message, room_id, is_dm=False)

            except Exception as e:
                logger.error(f"Error polling {channel_name}: {e}")

    def poll_dms(self):
        """Poll for new messages in DMs."""
        for room_id in self.dm_room_ids:
            try:
                result = self.rc.im_history(room_id=room_id, count=10)

                if result.ok:
                    messages = result.json().get("messages", [])

                    # Process oldest first
                    for message in reversed(messages):
                        if self.should_respond(message, is_dm=True):
                            self.process_message(message, room_id, is_dm=True)

            except Exception as e:
                logger.debug(f"Error polling DM {room_id}: {e}")

    def run(self):
        """Main bot loop."""
        logger.info("Starting Rocket.Chat <-> Dify bot...")

        if not self.connect():
            return

        if not self.setup_channels():
            return

        self.setup_dms()

        logger.info(f"Bot ready. Polling every {POLL_INTERVAL}s")
        logger.info(f"Prefix: '{RC_PREFIX}' (empty = respond to all)")
        logger.info(f"Allowed users: {RC_ALLOWED_USERS or 'all'}")

        reconnect_attempts = 0
        max_reconnect = 5

        while True:
            try:
                self.poll_messages()
                self.poll_dms()
                reconnect_attempts = 0

            except Exception as e:
                logger.error(f"Poll error: {e}")
                reconnect_attempts += 1

                if reconnect_attempts >= max_reconnect:
                    logger.error("Too many errors, reconnecting...")
                    time.sleep(10)
                    if self.connect():
                        reconnect_attempts = 0
                    else:
                        time.sleep(30)

            time.sleep(POLL_INTERVAL)


# =============================================================================
# Main
# =============================================================================

def validate_config() -> bool:
    """Validate required configuration."""
    errors = []

    if not RC_URL or "CHANGE_THIS" in RC_URL:
        errors.append("RC_URL not configured")
    if not RC_USERNAME:
        errors.append("RC_USERNAME not configured")
    if not RC_PASSWORD or "CHANGE_THIS" in RC_PASSWORD:
        errors.append("RC_PASSWORD not configured")
    if not RC_CHANNELS:
        errors.append("RC_CHANNELS not configured")
    if not OLLAMA_URL:
        errors.append("OLLAMA_URL not configured")
    if not OLLAMA_MODEL:
        errors.append("OLLAMA_MODEL not configured")
    if not ZABBIX_PROXY_TOKEN or "CHANGE_THIS" in ZABBIX_PROXY_TOKEN:
        errors.append("ZABBIX_PROXY_TOKEN not configured")

    if errors:
        logger.error("Configuration errors:")
        for err in errors:
            logger.error(f"  - {err}")
        logger.error("")
        logger.error("Set these in .env file (copy from .env.example)")
        return False

    return True


def main():
    """Entry point."""
    if not validate_config():
        sys.exit(1)

    bot = RocketChatBot()
    bot.run()


if __name__ == "__main__":
    main()

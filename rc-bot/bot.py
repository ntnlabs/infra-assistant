#!/usr/bin/env python3
"""
Rocket.Chat <-> Dify Bridge Bot
===============================
Forwards messages from Rocket.Chat to Dify and sends responses back.

Configuration via environment variables (or .env file):
- RC_URL, RC_USERNAME, RC_PASSWORD
- RC_CHANNELS, RC_ALLOWED_USERS, RC_PREFIX
- DIFY_URL, DIFY_API_KEY
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

# Dify
DIFY_URL = os.environ.get("DIFY_URL", "http://localhost/v1")
DIFY_API_KEY = os.environ.get("DIFY_API_KEY", "")

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

# Conversation tracking: room_id -> {"dify_conversation_id": str, "last_activity": datetime}
conversations: dict = {}

# Track processed message IDs to avoid duplicates
processed_messages: set = set()
MAX_PROCESSED_MESSAGES = 10000

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

    def get_conversation_id(self, room_id: str, user: str) -> str:
        """Get or create conversation ID for a room + user."""
        conv_key = f"{room_id}:{user}"

        if conv_key in conversations:
            conv = conversations[conv_key]

            # Check if conversation is still active
            if CONVERSATION_TIMEOUT > 0:
                elapsed = datetime.now() - conv["last_activity"]
                if elapsed > timedelta(seconds=CONVERSATION_TIMEOUT):
                    logger.info(f"Conversation expired for {user} in room {room_id}")
                    del conversations[conv_key]
                    return ""

            return conv.get("dify_conversation_id", "")

        return ""

    def update_conversation(self, room_id: str, user: str, dify_conv_id: str):
        """Update conversation tracking."""
        conv_key = f"{room_id}:{user}"
        conversations[conv_key] = {
            "dify_conversation_id": dify_conv_id,
            "last_activity": datetime.now()
        }

    def call_dify(self, text: str, room_id: str, user: str) -> str:
        """Send message to Dify and get response (streaming mode for Agent apps)."""
        import json as json_lib
        conversation_id = self.get_conversation_id(room_id, user)

        try:
            response = requests.post(
                f"{DIFY_URL}/chat-messages",
                headers={
                    "Authorization": f"Bearer {DIFY_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "inputs": {},
                    "query": text,
                    "response_mode": "streaming",
                    "conversation_id": conversation_id,
                    "user": user
                },
                timeout=120,
                stream=True
            )

            if response.status_code == 401:
                logger.error("Dify API key is invalid!")
                return "Error: Invalid Dify API key."

            if response.status_code == 404:
                logger.error("Dify app not found - check DIFY_URL")
                return "Error: Dify app not found."

            response.raise_for_status()

            # Parse streaming response (Server-Sent Events format)
            full_answer = ""
            for line in response.iter_lines():
                if line:
                    line = line.decode('utf-8')
                    if line.startswith('data: '):
                        try:
                            data = json_lib.loads(line[6:])
                            event = data.get('event', '')

                            # Collect answer chunks from any event that has 'answer'
                            if 'answer' in data:
                                answer_chunk = data.get('answer', '')
                                if answer_chunk and not answer_chunk.startswith('{'):
                                    # Only add non-JSON text (skip raw tool calls)
                                    full_answer += answer_chunk

                            # Also check for final message content
                            if event in ['agent_message', 'message']:
                                content = data.get('answer', '')
                                if content and not content.startswith('{'):
                                    if content not in full_answer:  # Avoid duplicates
                                        full_answer += content

                            # Update conversation ID
                            if 'conversation_id' in data:
                                self.update_conversation(room_id, user, data['conversation_id'])

                        except json_lib.JSONDecodeError:
                            pass

            return full_answer.strip() or "No response from assistant."

        except requests.exceptions.Timeout:
            logger.error("Dify request timed out")
            return "Request timed out. The assistant is taking too long."

        except requests.exceptions.ConnectionError:
            logger.error("Cannot connect to Dify")
            return "Error: Cannot connect to Dify service."

        except requests.exceptions.RequestException as e:
            logger.error(f"Dify API error: {e}")
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

        # Get response from Dify
        response = self.call_dify(text, room_id, user)

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
    if not DIFY_API_KEY or "CHANGE_THIS" in DIFY_API_KEY:
        errors.append("DIFY_API_KEY not configured")

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

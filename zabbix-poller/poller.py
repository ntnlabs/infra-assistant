#!/usr/bin/env python3
"""
Zabbix Alert Poller
===================
Periodically checks for new Zabbix alerts and posts to Rocket.Chat.

Tracks seen alerts to avoid duplicates.
"""

import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

import requests

# Try to load .env file
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass

# =============================================================================
# Configuration
# =============================================================================

ZABBIX_PROXY_URL = os.environ.get("ZABBIX_PROXY_URL", "http://localhost:5002")
ZABBIX_PROXY_TOKEN = os.environ.get("ZABBIX_PROXY_TOKEN", "")
RC_WEBHOOK_URL = os.environ.get("RC_ALERT_WEBHOOK_URL", "")
MIN_SEVERITY = int(os.environ.get("ALERT_MIN_SEVERITY", "4"))  # 3=Average, 4=High, 5=Disaster
STATE_FILE = Path(__file__).parent / "seen_alerts.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

SEVERITY_EMOJI = {
    0: "ℹ️",
    1: "ℹ️",
    2: "⚠️",
    3: "⚠️",
    4: "🔴",
    5: "🚨"
}

SEVERITY_NAMES = [
    "Not classified",
    "Information",
    "Warning",
    "Average",
    "High",
    "Disaster"
]

# =============================================================================
# State Management
# =============================================================================

def load_seen_alerts():
    """Load previously seen alert IDs."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return set(json.load(f))
        except Exception as e:
            logger.error(f"Error loading state: {e}")
    return set()


def save_seen_alerts(alert_ids: set):
    """Save seen alert IDs."""
    try:
        # Keep only last 10000 IDs to prevent file growth
        if len(alert_ids) > 10000:
            alert_ids = set(list(alert_ids)[-10000:])

        with open(STATE_FILE, 'w') as f:
            json.dump(list(alert_ids), f)
    except Exception as e:
        logger.error(f"Error saving state: {e}")


# =============================================================================
# Zabbix API
# =============================================================================

def get_active_problems():
    """Get active problems from Zabbix proxy."""
    try:
        response = requests.get(
            f"{ZABBIX_PROXY_URL}/problems",
            headers={"Authorization": f"Bearer {ZABBIX_PROXY_TOKEN}"},
            params={"severity": MIN_SEVERITY, "limit": 100},
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        return data.get("problems", [])
    except Exception as e:
        logger.error(f"Error getting problems: {e}")
        return []


# =============================================================================
# Alert Posting
# =============================================================================

def post_alerts_to_rc(alerts: list):
    """Post alerts to Rocket.Chat."""
    if not RC_WEBHOOK_URL:
        logger.warning("RC_ALERT_WEBHOOK_URL not configured, skipping RC post")
        return

    # Format alerts
    message = f"🚨 **New Alerts Detected** ({len(alerts)})\n\n"
    for alert in alerts:
        severity = alert.get("severity", 0)
        severity_name = SEVERITY_NAMES[severity] if 0 <= severity <= 5 else "Unknown"
        emoji = SEVERITY_EMOJI.get(severity, "⚠️")
        message += f"{emoji} **[{severity_name}]** {alert.get('name', 'Unknown')} on {alert.get('hostname', 'Unknown')}\n"

    try:
        response = requests.post(
            RC_WEBHOOK_URL,
            json={"text": message},
            timeout=10
        )
        response.raise_for_status()
        logger.info(f"Posted {len(alerts)} alerts to RC")
    except Exception as e:
        logger.error(f"Error posting to RC: {e}")


# =============================================================================
# Main Logic
# =============================================================================

def main():
    """Main poller logic."""
    logger.info("Checking for new alerts...")

    # Load seen alerts
    seen_alerts = load_seen_alerts()
    logger.debug(f"Tracking {len(seen_alerts)} previously seen alerts")

    # Get current problems
    problems = get_active_problems()

    if not problems:
        logger.info("No active problems")
        return

    logger.info(f"Found {len(problems)} active problems")

    # Find new alerts
    new_alerts = []
    current_alert_ids = set()

    for problem in problems:
        event_id = problem.get("eventid")
        current_alert_ids.add(event_id)

        if event_id not in seen_alerts:
            new_alerts.append(problem)

    # Post new alerts
    if new_alerts:
        logger.info(f"Found {len(new_alerts)} new alerts, posting to RC")
        post_alerts_to_rc(new_alerts)
    else:
        logger.info("No new alerts")

    # Update state with all current alerts
    # This handles alerts that got resolved and came back
    save_seen_alerts(current_alert_ids)


if __name__ == "__main__":
    if not ZABBIX_PROXY_TOKEN:
        logger.error("ZABBIX_PROXY_TOKEN not configured")
        sys.exit(1)

    main()

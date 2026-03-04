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
DIFY_URL = os.environ.get("DIFY_URL", "http://localhost/v1")
DIFY_API_KEY = os.environ.get("DIFY_API_KEY", "")
RC_WEBHOOK_URL = os.environ.get("RC_ALERT_WEBHOOK_URL", "")
MIN_SEVERITY = int(os.environ.get("ALERT_MIN_SEVERITY", "3"))  # 3=Average, 4=High, 5=Disaster
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
# Dify Analysis
# =============================================================================

def analyze_alerts_with_dify(alerts: list):
    """Send alerts to Dify for LLM analysis and post result to RC."""
    if not DIFY_API_KEY:
        logger.error("DIFY_API_KEY not configured")
        return

    if not RC_WEBHOOK_URL:
        logger.warning("RC_ALERT_WEBHOOK_URL not configured, skipping RC post")
        return

    # Format alerts for LLM
    alert_summary = f"New alerts detected ({len(alerts)}):\n\n"
    for alert in alerts:
        severity = alert.get("severity", 0)
        severity_name = SEVERITY_NAMES[severity] if 0 <= severity <= 5 else "Unknown"
        alert_summary += f"- [{severity_name}] {alert['name']} on {alert['hostname']}\n"

    # Ask LLM to analyze
    prompt = f"{alert_summary}\n\nAnalyze these new alerts. Identify patterns, assess severity, and recommend actions."

    try:
        # Call Dify (non-streaming for cron)
        response = requests.post(
            f"{DIFY_URL}/chat-messages",
            headers={
                "Authorization": f"Bearer {DIFY_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "inputs": {},
                "query": prompt,
                "response_mode": "blocking",
                "user": "zabbix-poller"
            },
            timeout=60
        )

        if response.status_code != 200:
            logger.error(f"Dify returned {response.status_code}")
            return

        data = response.json()
        analysis = data.get("answer", "No analysis available")

        # Post analysis to RC
        post_analysis_to_rc(analysis, len(alerts))

    except Exception as e:
        logger.error(f"Error analyzing with Dify: {e}")


def post_analysis_to_rc(analysis: str, alert_count: int):
    """Post LLM analysis to Rocket.Chat."""
    message = f"🔍 **Alert Analysis** ({alert_count} new alerts)\n\n{analysis}"

    try:
        response = requests.post(
            RC_WEBHOOK_URL,
            json={"text": message},
            timeout=10
        )
        response.raise_for_status()
        logger.info(f"Posted analysis to RC")
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

    # Analyze new alerts with LLM
    if new_alerts:
        logger.info(f"Found {len(new_alerts)} new alerts, sending to Dify for analysis")
        analyze_alerts_with_dify(new_alerts)
    else:
        logger.info("No new alerts")

    # Update state with all current alerts
    # This handles alerts that got resolved and came back
    save_seen_alerts(current_alert_ids)


if __name__ == "__main__":
    if not ZABBIX_PROXY_TOKEN:
        logger.error("ZABBIX_PROXY_TOKEN not configured")
        sys.exit(1)

    if not DIFY_API_KEY:
        logger.error("DIFY_API_KEY not configured")
        sys.exit(1)

    main()

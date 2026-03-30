#!/usr/bin/env python3
"""
Zammad Unassigned Ticket Poller
================================
Long-running service that checks for open, unassigned helpdesk tickets
and posts alerts to Rocket.Chat when tickets go unattended too long.

Tracks notified ticket IDs to avoid duplicate alerts.
When a ticket gets assigned or closed, it is removed from state.
"""

import os
import sys
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone

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

ZAMMAD_PROXY_URL = os.environ.get("ZAMMAD_PROXY_URL", "http://localhost:5003").rstrip("/")
ZAMMAD_PROXY_TOKEN = os.environ.get("ZAMMAD_PROXY_TOKEN", "")
RC_WEBHOOK_URL = os.environ.get("RC_ALERT_WEBHOOK_URL", "")
THRESHOLD_MINUTES = int(os.environ.get("ZAMMAD_UNASSIGNED_THRESHOLD_MINUTES", "60"))
POLL_INTERVAL = int(os.environ.get("ZAMMAD_POLL_INTERVAL", "300"))
STATE_FILE = Path(__file__).parent / "notified_tickets.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# =============================================================================
# State Management
# =============================================================================

def load_notified_tickets() -> set:
    """Load set of ticket IDs we've already alerted on."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return set(json.load(f))
        except Exception as e:
            logger.error(f"Error loading state: {e}")
    return set()


def save_notified_tickets(ticket_ids: set):
    """Persist the set of notified ticket IDs."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(list(ticket_ids), f)
    except Exception as e:
        logger.error(f"Error saving state: {e}")

# =============================================================================
# Zammad Proxy
# =============================================================================

def get_open_tickets() -> list:
    """Fetch open tickets from the Zammad proxy."""
    try:
        response = requests.get(
            f"{ZAMMAD_PROXY_URL}/tickets",
            headers={"Authorization": f"Bearer {ZAMMAD_PROXY_TOKEN}"},
            params={"state": "open", "limit": 200},
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        return data.get("tickets", [])
    except Exception as e:
        logger.error(f"Error fetching open tickets: {e}")
        return []

# =============================================================================
# Alert Formatting
# =============================================================================

def _format_age(age_minutes: int) -> str:
    """Format minutes as human-readable age string."""
    if age_minutes < 60:
        return f"{age_minutes}m ago"
    hours = age_minutes // 60
    mins = age_minutes % 60
    if mins:
        return f"{hours}h {mins:02d}m ago"
    return f"{hours}h ago"


def post_alert_to_rc(qualifying_tickets: list):
    """Post unassigned ticket alert to Rocket.Chat webhook."""
    if not RC_WEBHOOK_URL:
        logger.warning("RC_ALERT_WEBHOOK_URL not configured, skipping RC post")
        return

    lines = [f"🎫 Unassigned helpdesk tickets (open > {THRESHOLD_MINUTES // 60 if THRESHOLD_MINUTES >= 60 else THRESHOLD_MINUTES}{'h' if THRESHOLD_MINUTES >= 60 else 'm'})\n"]
    for t in qualifying_tickets:
        number = t.get("number") or t.get("id", "?")
        title = t.get("title", "No title")
        age_minutes = t.get("age_minutes")
        age_str = _format_age(age_minutes) if age_minutes is not None else "unknown age"
        lines.append(f"#{number} — {title} ({age_str})")

    message = "\n".join(lines)

    try:
        response = requests.post(
            RC_WEBHOOK_URL,
            json={"text": message},
            timeout=10
        )
        response.raise_for_status()
        logger.info(f"Posted alert for {len(qualifying_tickets)} ticket(s) to RC")
    except Exception as e:
        logger.error(f"Error posting to RC: {e}")

# =============================================================================
# Main Logic
# =============================================================================

def main():
    """One poll cycle: check tickets, alert on newly-qualifying ones."""
    logger.info("Checking for unassigned tickets...")

    notified = load_notified_tickets()

    tickets = get_open_tickets()
    if not tickets:
        logger.info("No open tickets returned")
        # Wipe state — if proxy is down, don't re-alert stale tickets on recovery
        save_notified_tickets(set())
        return

    logger.info(f"Got {len(tickets)} open ticket(s)")

    # Tickets qualifying for alert: unassigned AND old enough
    qualifying = []
    current_qualifying_ids = set()

    for t in tickets:
        ticket_id = str(t.get("id", ""))
        if not ticket_id:
            continue

        owner_id = t.get("owner_id")
        age_minutes = t.get("age_minutes")

        # owner_id of 1 often means "unassigned" in Zammad (system user)
        # A value of None or falsy also means unassigned
        # We treat both None and 1 as unassigned
        is_unassigned = not owner_id or owner_id == 1

        if not is_unassigned:
            continue

        if age_minutes is None or age_minutes < THRESHOLD_MINUTES:
            continue

        current_qualifying_ids.add(ticket_id)

        if ticket_id not in notified:
            qualifying.append(t)

    # Post alert for newly qualifying tickets
    if qualifying:
        logger.info(f"Found {len(qualifying)} newly qualifying ticket(s), alerting")
        post_alert_to_rc(qualifying)
    else:
        logger.info("No new qualifying tickets")

    # Update state: keep only IDs still qualifying (removes assigned/closed tickets)
    new_notified = notified & current_qualifying_ids  # keep only still-qualifying
    new_notified |= {str(t.get("id", "")) for t in qualifying}  # add newly alerted

    save_notified_tickets(new_notified)
    logger.info(f"State: tracking {len(new_notified)} notified ticket(s)")


if __name__ == "__main__":
    if not ZAMMAD_PROXY_TOKEN:
        logger.error("ZAMMAD_PROXY_TOKEN not configured")
        sys.exit(1)

    logger.info(f"Zammad poller starting — interval={POLL_INTERVAL}s, threshold={THRESHOLD_MINUTES}min")

    while True:
        try:
            main()
        except Exception as e:
            logger.error(f"Unexpected error in poll cycle: {e}")
        time.sleep(POLL_INTERVAL)

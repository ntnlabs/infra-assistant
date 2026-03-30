#!/usr/bin/env python3
"""
Zammad Proxy Service
====================
Provides REST API endpoints for Zammad helpdesk API.
Handles authentication and translates REST calls to Zammad format.

Endpoints:
- GET  /health              - Health check
- GET  /tickets             - List tickets (params: state, group, limit)
- GET  /tickets/<id>        - Single ticket with articles
- GET  /tickets/search      - Full-text search (param: q)
- PATCH /tickets/<id>/state - Update ticket state
- POST /tickets/<id>/note   - Add internal note
"""

import os
import sys
import hmac
import logging
from pathlib import Path
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import Flask, request, jsonify

# Try to load .env file
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass

app = Flask(__name__)

# =============================================================================
# Configuration
# =============================================================================

ZAMMAD_URL = os.environ.get("ZAMMAD_URL", "").rstrip("/")
ZAMMAD_TOKEN = os.environ.get("ZAMMAD_TOKEN", "")
ZAMMAD_PROXY_PORT = int(os.environ.get("ZAMMAD_PROXY_PORT", "5003"))
ZAMMAD_PROXY_TOKEN = os.environ.get("ZAMMAD_PROXY_TOKEN", "")
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

# =============================================================================
# Logging
# =============================================================================

log_level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# =============================================================================
# Zammad API Client
# =============================================================================

class ZammadClient:
    """Thin wrapper around Zammad REST API."""

    def __init__(self):
        self.base_url = f"{ZAMMAD_URL}/api/v1"
        self.headers = {
            "Authorization": f"Token token={ZAMMAD_TOKEN}",
            "Content-Type": "application/json"
        }

    def _get(self, path: str, params: dict = None) -> dict:
        """GET request to Zammad API."""
        response = requests.get(
            f"{self.base_url}{path}",
            headers=self.headers,
            params=params or {},
            timeout=30
        )
        response.raise_for_status()
        return response.json()

    def _patch(self, path: str, data: dict) -> dict:
        """PATCH request to Zammad API."""
        response = requests.patch(
            f"{self.base_url}{path}",
            headers=self.headers,
            json=data,
            timeout=30
        )
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, data: dict) -> dict:
        """POST request to Zammad API."""
        response = requests.post(
            f"{self.base_url}{path}",
            headers=self.headers,
            json=data,
            timeout=30
        )
        response.raise_for_status()
        return response.json()


# Global client instance
zammad = ZammadClient()

# =============================================================================
# Auth Decorator
# =============================================================================

def require_auth(f):
    """Require bearer token for endpoint."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not ZAMMAD_PROXY_TOKEN:
            return jsonify({"error": "Service not configured (ZAMMAD_PROXY_TOKEN missing)"}), 503

        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not hmac.compare_digest(token, ZAMMAD_PROXY_TOKEN):
            return jsonify({"error": "Unauthorized"}), 401

        return f(*args, **kwargs)
    return decorated

# =============================================================================
# Helpers
# =============================================================================

def _format_ticket(ticket: dict) -> dict:
    """Format a Zammad ticket for API response."""
    created_at = ticket.get("created_at", "")
    updated_at = ticket.get("updated_at", "")

    # Calculate age in minutes if created_at available
    age_minutes = None
    try:
        if created_at:
            # Zammad uses ISO 8601 with timezone
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            age_minutes = int((now - created_dt).total_seconds() / 60)
    except Exception:
        pass

    return {
        "id": ticket.get("id"),
        "number": ticket.get("number"),
        "title": ticket.get("title"),
        "state": ticket.get("state"),
        "state_id": ticket.get("state_id"),
        "priority": ticket.get("priority"),
        "group": ticket.get("group"),
        "owner": ticket.get("owner"),
        "owner_id": ticket.get("owner_id"),
        "customer": ticket.get("customer"),
        "customer_id": ticket.get("customer_id"),
        "created_at": created_at,
        "updated_at": updated_at,
        "age_minutes": age_minutes,
        "tags": ticket.get("tags", []),
    }


# Map Zammad state names to filter
OPEN_STATES = {"new", "open"}
STATE_FILTER_MAP = {
    "open": ["new", "open"],
    "closed": ["closed"],
    "pending": ["pending reminder", "pending close"],
    "all": None,  # No filter
}

# =============================================================================
# Routes
# =============================================================================

@app.route("/health", methods=["GET"])
def health():
    """Health check — calls /api/v1/users/me."""
    try:
        me = zammad._get("/users/me")
        return jsonify({
            "status": "ok",
            "zammad_url": ZAMMAD_URL,
            "user": me.get("login", "unknown")
        })
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({"status": "error", "error": str(e)}), 503


@app.route("/tickets", methods=["GET"])
@require_auth
def list_tickets():
    """List tickets.

    Query params:
    - state: open (default), closed, pending, all
    - group: filter by group name
    - limit: max results (default 25)
    """
    try:
        try:
            limit = max(1, min(200, int(request.args.get("limit", 25))))
        except ValueError:
            return jsonify({"error": "Invalid limit parameter"}), 400

        state_filter = request.args.get("state", "open").lower()
        group_filter = request.args.get("group", "").strip()

        # Build search query for Zammad
        # Use search endpoint with state filter for flexibility
        state_names = STATE_FILTER_MAP.get(state_filter)

        if state_names is not None:
            # Build a query string using Zammad search syntax
            state_query = " OR ".join(f'state.name:"{s}"' for s in state_names)
            query = f"({state_query})"
        else:
            query = "*"

        if group_filter:
            query += f' AND group.name:"{group_filter}"'

        params = {
            "query": query,
            "limit": limit,
            "sort_by": "created_at",
            "order_by": "desc",
        }

        data = zammad._get("/tickets/search", params=params)

        # search returns {"tickets": [...], "tickets_count": N} or list depending on version
        if isinstance(data, list):
            tickets = data
        elif isinstance(data, dict):
            ticket_ids = data.get("tickets", [])
            ticket_records = data.get("assets", {}).get("Ticket", {})
            if ticket_records:
                tickets = list(ticket_records.values())
            else:
                # Fall back: fetch each ticket individually
                tickets = []
                for tid in ticket_ids[:limit]:
                    try:
                        t = zammad._get(f"/tickets/{tid}", params={"expand": "true"})
                        tickets.append(t)
                    except Exception:
                        pass
        else:
            tickets = []

        formatted = [_format_ticket(t) for t in tickets[:limit]]

        return jsonify({
            "count": len(formatted),
            "tickets": formatted
        })

    except Exception as e:
        logger.error(f"Error listing tickets: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/tickets/search", methods=["GET"])
@require_auth
def search_tickets():
    """Full-text ticket search.

    Query params:
    - q: search query (required)
    - limit: max results (default 25)
    """
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "q parameter is required"}), 400

    try:
        try:
            limit = max(1, min(200, int(request.args.get("limit", 25))))
        except ValueError:
            return jsonify({"error": "Invalid limit parameter"}), 400

        params = {
            "query": q,
            "limit": limit,
        }

        data = zammad._get("/tickets/search", params=params)

        if isinstance(data, list):
            tickets = data
        elif isinstance(data, dict):
            ticket_ids = data.get("tickets", [])
            ticket_records = data.get("assets", {}).get("Ticket", {})
            if ticket_records:
                tickets = list(ticket_records.values())
            else:
                tickets = []
                for tid in ticket_ids[:limit]:
                    try:
                        t = zammad._get(f"/tickets/{tid}", params={"expand": "true"})
                        tickets.append(t)
                    except Exception:
                        pass
        else:
            tickets = []

        formatted = [_format_ticket(t) for t in tickets[:limit]]

        return jsonify({
            "query": q,
            "count": len(formatted),
            "tickets": formatted
        })

    except Exception as e:
        logger.error(f"Error searching tickets: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/tickets/<int:ticket_id>", methods=["GET"])
@require_auth
def get_ticket(ticket_id: int):
    """Get a single ticket with its articles."""
    try:
        ticket = zammad._get(f"/tickets/{ticket_id}", params={"expand": "true"})
        articles_data = zammad._get(f"/ticket_articles/by_ticket/{ticket_id}")

        # Format articles
        articles = []
        if isinstance(articles_data, list):
            raw_articles = articles_data
        elif isinstance(articles_data, dict):
            raw_articles = articles_data.get("ticket_articles", [])
        else:
            raw_articles = []

        for a in raw_articles:
            articles.append({
                "id": a.get("id"),
                "from": a.get("from"),
                "to": a.get("to"),
                "subject": a.get("subject"),
                "body": a.get("body"),
                "content_type": a.get("content_type"),
                "type": a.get("type"),
                "internal": a.get("internal", False),
                "created_at": a.get("created_at"),
            })

        result = _format_ticket(ticket)
        result["articles"] = articles

        return jsonify(result)

    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return jsonify({"error": f"Ticket {ticket_id} not found"}), 404
        logger.error(f"Error getting ticket {ticket_id}: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Error getting ticket {ticket_id}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/tickets/<int:ticket_id>/state", methods=["PATCH"])
@require_auth
def update_ticket_state(ticket_id: int):
    """Update ticket state.

    Body: {"state": "closed"}
    Valid states: open, closed, pending reminder, pending close, new
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    new_state = data.get("state", "").strip()
    if not new_state:
        return jsonify({"error": "state field required"}), 400

    valid_states = {"open", "closed", "new", "pending reminder", "pending close"}
    if new_state not in valid_states:
        return jsonify({"error": f"Invalid state '{new_state}'. Valid: {', '.join(sorted(valid_states))}"}), 400

    try:
        result = zammad._patch(f"/tickets/{ticket_id}", {"state": new_state})
        return jsonify({
            "success": True,
            "ticket_id": ticket_id,
            "state": result.get("state"),
            "message": f"Ticket {ticket_id} state updated to '{new_state}'"
        })

    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return jsonify({"error": f"Ticket {ticket_id} not found"}), 404
        logger.error(f"Error updating ticket {ticket_id} state: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Error updating ticket {ticket_id} state: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/tickets/<int:ticket_id>/note", methods=["POST"])
@require_auth
def add_ticket_note(ticket_id: int):
    """Add an internal note to a ticket.

    Body: {"body": "Note text here"}
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    body = data.get("body", "").strip()
    if not body:
        return jsonify({"error": "body field required"}), 400

    try:
        result = zammad._post("/ticket_articles", {
            "ticket_id": ticket_id,
            "body": body,
            "internal": True,
            "type": "note",
            "content_type": "text/plain",
        })

        return jsonify({
            "success": True,
            "ticket_id": ticket_id,
            "article_id": result.get("id"),
            "message": f"Note added to ticket {ticket_id}"
        })

    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return jsonify({"error": f"Ticket {ticket_id} not found"}), 404
        logger.error(f"Error adding note to ticket {ticket_id}: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Error adding note to ticket {ticket_id}: {e}")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    if not ZAMMAD_URL:
        logger.error("ZAMMAD_URL not configured")
        sys.exit(1)
    if not ZAMMAD_TOKEN:
        logger.error("ZAMMAD_TOKEN not configured")
        sys.exit(1)
    if not ZAMMAD_PROXY_TOKEN:
        logger.error("ZAMMAD_PROXY_TOKEN not configured")
        sys.exit(1)

    logger.info(f"Starting Zammad Proxy on port {ZAMMAD_PROXY_PORT}")
    app.run(host="0.0.0.0", port=ZAMMAD_PROXY_PORT, debug=DEBUG)

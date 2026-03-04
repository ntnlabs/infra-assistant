#!/usr/bin/env python3
"""
Zabbix Proxy Service
====================
Provides REST API endpoints for Zabbix JSON-RPC API.
Handles authentication and translates REST calls to Zabbix format.

Endpoints:
- GET /health - Health check
- GET /problems - Get active problems
- GET /hosts - Get monitored hosts
- GET /host/<name>/problems - Get problems for specific host
- GET /triggers - Get triggers
- POST /acknowledge - Acknowledge or close events
"""

import os
import sys
import logging
from pathlib import Path
from datetime import datetime
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

ZABBIX_URL = os.environ.get("ZABBIX_URL", "").rstrip("/")
ZABBIX_USER = os.environ.get("ZABBIX_USER", "")
ZABBIX_PASSWORD = os.environ.get("ZABBIX_PASSWORD", "")
ZABBIX_PROXY_PORT = int(os.environ.get("ZABBIX_PROXY_PORT", "5002"))
ZABBIX_PROXY_TOKEN = os.environ.get("ZABBIX_PROXY_TOKEN", "")
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

# Zabbix API endpoint
ZABBIX_API = f"{ZABBIX_URL}/api_jsonrpc.php"

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
# Zabbix API Client
# =============================================================================

class ZabbixClient:
    """Simple Zabbix API client."""

    def __init__(self):
        self.auth_token = None
        self.request_id = 1

    def _call(self, method: str, params: dict = None) -> dict:
        """Make a Zabbix API call."""
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self.request_id
        }

        # Add auth token if we have one (not needed for user.login)
        if self.auth_token and method != "user.login":
            payload["auth"] = self.auth_token

        self.request_id += 1

        try:
            response = requests.post(
                ZABBIX_API,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=30
            )
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                error = data["error"]
                raise Exception(f"Zabbix API error: {error.get('message', '')} - {error.get('data', '')}")

            return data.get("result")

        except requests.exceptions.RequestException as e:
            logger.error(f"Zabbix API request failed: {e}")
            raise

    def login(self) -> bool:
        """Authenticate with Zabbix."""
        try:
            self.auth_token = self._call("user.login", {
                "user": ZABBIX_USER,
                "password": ZABBIX_PASSWORD
            })
            logger.info("Authenticated with Zabbix")
            return True
        except Exception as e:
            logger.error(f"Zabbix login failed: {e}")
            return False

    def ensure_auth(self):
        """Ensure we have a valid auth token."""
        if not self.auth_token:
            if not self.login():
                raise Exception("Failed to authenticate with Zabbix")

    def get_problems(self, severity_min: int = 0, limit: int = 100) -> list:
        """Get active problems."""
        self.ensure_auth()
        return self._call("problem.get", {
            "output": "extend",
            "selectHosts": ["host", "name"],
            "selectTags": "extend",
            "recent": True,
            "sortfield": ["eventid"],
            "sortorder": "DESC",
            "limit": limit,
            "severities": list(range(severity_min, 6))  # severity_min to 5 (disaster)
        })

    def get_hosts(self, group: str = None) -> list:
        """Get monitored hosts."""
        self.ensure_auth()
        params = {
            "output": ["hostid", "host", "name", "status", "available"],
            "selectGroups": ["name"],
            "selectInterfaces": ["ip", "dns"],
            "filter": {"status": 0}  # Only monitored hosts
        }
        if group:
            # Get group ID first
            groups = self._call("hostgroup.get", {
                "output": ["groupid"],
                "filter": {"name": group}
            })
            if groups:
                params["groupids"] = [g["groupid"] for g in groups]

        return self._call("host.get", params)

    def get_host_problems(self, hostname: str) -> list:
        """Get problems for a specific host."""
        self.ensure_auth()

        # Get host ID
        hosts = self._call("host.get", {
            "output": ["hostid"],
            "filter": {"host": hostname}
        })

        if not hosts:
            # Try searching by name
            hosts = self._call("host.get", {
                "output": ["hostid"],
                "search": {"name": hostname}
            })

        if not hosts:
            return []

        host_ids = [h["hostid"] for h in hosts]

        return self._call("problem.get", {
            "output": "extend",
            "hostids": host_ids,
            "selectHosts": ["host", "name"],
            "recent": True,
            "sortfield": ["eventid"],
            "sortorder": "DESC"
        })

    def get_triggers(self, only_problems: bool = True, limit: int = 100) -> list:
        """Get triggers."""
        self.ensure_auth()
        params = {
            "output": ["triggerid", "description", "priority", "value", "lastchange"],
            "selectHosts": ["host", "name"],
            "sortfield": "lastchange",
            "sortorder": "DESC",
            "limit": limit
        }
        if only_problems:
            params["filter"] = {"value": 1}  # Only triggers in problem state

        return self._call("trigger.get", params)

    def get_events(self, time_from: int = None, limit: int = 50) -> list:
        """Get recent events."""
        self.ensure_auth()
        params = {
            "output": "extend",
            "selectHosts": ["host", "name"],
            "sortfield": ["clock", "eventid"],
            "sortorder": "DESC",
            "limit": limit
        }
        if time_from:
            params["time_from"] = time_from

        return self._call("event.get", params)


# Global client instance
zabbix = ZabbixClient()

# =============================================================================
# Auth Decorator
# =============================================================================

def require_auth(f):
    """Require API token for endpoint."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not ZABBIX_PROXY_TOKEN:
            return f(*args, **kwargs)  # No token configured, allow all

        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != ZABBIX_PROXY_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401

        return f(*args, **kwargs)
    return decorated

# =============================================================================
# Routes
# =============================================================================

@app.route("/health", methods=["GET"])
def health():
    """Health check."""
    return jsonify({
        "status": "ok",
        "zabbix_url": ZABBIX_URL,
        "authenticated": zabbix.auth_token is not None
    })


@app.route("/problems", methods=["GET"])
@require_auth
def get_problems():
    """Get active problems.

    Query params:
    - severity: minimum severity (0-5, default 0)
    - limit: max results (default 100)
    """
    try:
        severity = int(request.args.get("severity", 0))
        limit = int(request.args.get("limit", 100))

        problems = zabbix.get_problems(severity_min=severity, limit=limit)

        # Format for readability
        formatted = []
        for p in problems:
            hosts = p.get("hosts", [])
            formatted.append({
                "eventid": p.get("eventid"),
                "severity": int(p.get("severity", 0)),
                "severity_name": ["Not classified", "Information", "Warning", "Average", "High", "Disaster"][int(p.get("severity", 0))],
                "name": p.get("name"),
                "host": hosts[0].get("host") if hosts else "Unknown",
                "hostname": hosts[0].get("name") if hosts else "Unknown",
                "acknowledged": p.get("acknowledged") == "1",
                "time": datetime.fromtimestamp(int(p.get("clock", 0))).isoformat(),
                "tags": p.get("tags", [])
            })

        return jsonify({
            "count": len(formatted),
            "problems": formatted
        })

    except Exception as e:
        logger.error(f"Error getting problems: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/hosts", methods=["GET"])
@require_auth
def get_hosts():
    """Get monitored hosts.

    Query params:
    - group: filter by host group name
    """
    try:
        group = request.args.get("group")
        hosts = zabbix.get_hosts(group=group)

        formatted = []
        for h in hosts:
            interfaces = h.get("interfaces", [])
            groups = h.get("groups", [])
            formatted.append({
                "hostid": h.get("hostid"),
                "host": h.get("host"),
                "name": h.get("name"),
                "status": "monitored" if h.get("status") == "0" else "unmonitored",
                "available": ["unknown", "available", "unavailable"][int(h.get("available", 0))],
                "ip": interfaces[0].get("ip") if interfaces else None,
                "groups": [g.get("name") for g in groups]
            })

        return jsonify({
            "count": len(formatted),
            "hosts": formatted
        })

    except Exception as e:
        logger.error(f"Error getting hosts: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/host/<hostname>/problems", methods=["GET"])
@require_auth
def get_host_problems(hostname: str):
    """Get problems for a specific host."""
    try:
        problems = zabbix.get_host_problems(hostname)

        formatted = []
        for p in problems:
            formatted.append({
                "eventid": p.get("eventid"),
                "severity": int(p.get("severity", 0)),
                "severity_name": ["Not classified", "Information", "Warning", "Average", "High", "Disaster"][int(p.get("severity", 0))],
                "name": p.get("name"),
                "acknowledged": p.get("acknowledged") == "1",
                "time": datetime.fromtimestamp(int(p.get("clock", 0))).isoformat()
            })

        return jsonify({
            "host": hostname,
            "count": len(formatted),
            "problems": formatted
        })

    except Exception as e:
        logger.error(f"Error getting host problems: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/triggers", methods=["GET"])
@require_auth
def get_triggers():
    """Get triggers.

    Query params:
    - problems_only: only show triggers in problem state (default true)
    - limit: max results (default 100)
    """
    try:
        problems_only = request.args.get("problems_only", "true").lower() == "true"
        limit = int(request.args.get("limit", 100))

        triggers = zabbix.get_triggers(only_problems=problems_only, limit=limit)

        formatted = []
        for t in triggers:
            hosts = t.get("hosts", [])
            formatted.append({
                "triggerid": t.get("triggerid"),
                "description": t.get("description"),
                "priority": int(t.get("priority", 0)),
                "priority_name": ["Not classified", "Information", "Warning", "Average", "High", "Disaster"][int(t.get("priority", 0))],
                "status": "problem" if t.get("value") == "1" else "ok",
                "host": hosts[0].get("host") if hosts else "Unknown",
                "last_change": datetime.fromtimestamp(int(t.get("lastchange", 0))).isoformat()
            })

        return jsonify({
            "count": len(formatted),
            "triggers": formatted
        })

    except Exception as e:
        logger.error(f"Error getting triggers: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/summary", methods=["GET"])
@require_auth
def get_summary():
    """Get a summary of current Zabbix status."""
    try:
        problems = zabbix.get_problems(limit=1000)

        # Count by severity
        severity_counts = {
            "disaster": 0,
            "high": 0,
            "average": 0,
            "warning": 0,
            "information": 0,
            "not_classified": 0
        }
        severity_names = ["not_classified", "information", "warning", "average", "high", "disaster"]

        for p in problems:
            sev = int(p.get("severity", 0))
            if 0 <= sev <= 5:
                severity_counts[severity_names[sev]] += 1

        # Get recent high-severity problems
        high_severity = [p for p in problems if int(p.get("severity", 0)) >= 4][:10]

        formatted_high = []
        for p in high_severity:
            hosts = p.get("hosts", [])
            formatted_high.append({
                "name": p.get("name"),
                "host": hosts[0].get("host") if hosts else "Unknown",
                "severity": ["Not classified", "Information", "Warning", "Average", "High", "Disaster"][int(p.get("severity", 0))],
                "time": datetime.fromtimestamp(int(p.get("clock", 0))).isoformat()
            })

        return jsonify({
            "total_problems": len(problems),
            "by_severity": severity_counts,
            "high_severity_problems": formatted_high
        })

    except Exception as e:
        logger.error(f"Error getting summary: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/acknowledge", methods=["POST"])
@require_token
def acknowledge_event():
    """
    Acknowledge or close Zabbix events.

    POST body:
    {
        "event_ids": ["12345", "12346"],
        "action": "acknowledge" | "close" | "acknowledge_with_message",
        "message": "Fixed by restarting service" (optional)
    }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body required"}), 400

        event_ids = data.get("event_ids", [])
        action_type = data.get("action", "acknowledge")
        message = data.get("message", "")

        if not event_ids:
            return jsonify({"error": "event_ids required"}), 400

        # Convert event_ids to strings
        event_ids = [str(eid) for eid in event_ids]

        # Map action type to Zabbix action bitmask
        # 1=close, 2=acknowledge, 4=add message, 8=change severity
        action_map = {
            "acknowledge": 2,
            "close": 1,
            "acknowledge_with_message": 6,  # acknowledge + message
            "close_with_message": 5  # close + message
        }

        action_value = action_map.get(action_type)
        if action_value is None:
            return jsonify({"error": f"Invalid action: {action_type}"}), 400

        # If message provided, add message flag
        if message and action_value in [1, 2]:
            action_value += 4

        # Call Zabbix API
        params = {
            "eventids": event_ids,
            "action": action_value
        }

        if message:
            params["message"] = message

        result = zabbix.api_call("event.acknowledge", params)

        if result:
            return jsonify({
                "success": True,
                "event_ids": result.get("eventids", []),
                "message": f"Successfully {action_type}d {len(event_ids)} event(s)"
            })
        else:
            return jsonify({"error": "Failed to acknowledge events"}), 500

    except Exception as e:
        logger.error(f"Error acknowledging events: {e}")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    if not ZABBIX_URL:
        logger.error("ZABBIX_URL not configured")
        sys.exit(1)
    if not ZABBIX_USER or not ZABBIX_PASSWORD:
        logger.error("ZABBIX_USER and ZABBIX_PASSWORD must be configured")
        sys.exit(1)

    # Test authentication on startup
    if not zabbix.login():
        logger.error("Failed to authenticate with Zabbix on startup")
        sys.exit(1)

    logger.info(f"Starting Zabbix Proxy on port {ZABBIX_PROXY_PORT}")
    app.run(host="0.0.0.0", port=ZABBIX_PROXY_PORT, debug=DEBUG)

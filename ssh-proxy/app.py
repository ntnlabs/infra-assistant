#!/usr/bin/env python3
"""
SSH Proxy Service
=================
Executes allowed SSH commands on allowed hosts.

Configuration:
- Environment variables (or .env file via python-dotenv)
- hosts.yaml - target hosts (copy from hosts.yaml.example)
- commands.yaml - allowed command patterns

Security features:
- Allowlist of hosts (hosts.yaml)
- Allowlist of command patterns (commands.yaml)
- API token authentication (from env)
- Full audit logging
"""

import re
import os
import sys
import yaml
import logging
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify
import paramiko

# Try to load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    # Look for .env in parent directory
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass  # python-dotenv not installed, use system env vars

app = Flask(__name__)

# =============================================================================
# Configuration from Environment
# =============================================================================

API_TOKEN = os.environ.get("SSH_PROXY_TOKEN", "")
LISTEN_PORT = int(os.environ.get("SSH_PROXY_PORT", "5001"))
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
# Load YAML Configs
# =============================================================================

BASE_DIR = Path(__file__).parent

def load_yaml(filename: str, example_filename: str = None) -> dict:
    """Load YAML file, with helpful error if missing."""
    filepath = BASE_DIR / filename
    example_path = BASE_DIR / (example_filename or f"{filename}.example")

    if not filepath.exists():
        if example_path.exists():
            logger.error(f"Config file missing: {filename}")
            logger.error(f"Copy the example: cp {example_filename or filename + '.example'} {filename}")
        else:
            logger.error(f"Config file missing: {filename}")
        return {}

    try:
        with open(filepath) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Failed to load {filename}: {e}")
        return {}

# Load configs
HOSTS_CONFIG = load_yaml("hosts.yaml", "hosts.yaml.example")
COMMANDS_CONFIG = load_yaml("commands.yaml")

ALLOWED_HOSTS = HOSTS_CONFIG.get("hosts", [])
ALLOWED_COMMANDS = COMMANDS_CONFIG.get("commands", [])

logger.info(f"Loaded {len(ALLOWED_HOSTS)} hosts, {len(ALLOWED_COMMANDS)} command patterns")

# =============================================================================
# Helper Functions
# =============================================================================

def get_host(name: str) -> dict | None:
    """Get host config by name."""
    for host in ALLOWED_HOSTS:
        if host.get("name") == name:
            return host
    return None


def is_command_allowed(command: str) -> tuple[bool, str]:
    """
    Check if command matches any allowed pattern.
    Returns (allowed, matched_pattern_description)
    """
    for allowed in ALLOWED_COMMANDS:
        pattern = allowed.get("pattern", "")
        try:
            if re.match(pattern, command):
                return True, allowed.get("description", "No description")
        except re.error as e:
            logger.error(f"Invalid regex pattern '{pattern}': {e}")
    return False, ""


def execute_ssh(host: dict, command: str) -> tuple[bool, str, float]:
    """
    Execute SSH command and return (success, output, duration_seconds).
    """
    start_time = datetime.now()

    try:
        client = paramiko.SSHClient()

        # AutoAddPolicy: Accept all host keys for internal infrastructure
        # Security is enforced via hosts.yaml allowlist, not host key verification
        # (Hashed known_hosts makes matching difficult, and this is internal/private network)
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        key_file = host.get("key_file")
        password = host.get("password")

        connect_kwargs = {
            "hostname": host["hostname"],
            "username": host["username"],
            "port": host.get("port", 22),
            "timeout": 30,
            "banner_timeout": 30,
        }

        if key_file:
            key_path = Path(key_file)
            if key_path.exists():
                connect_kwargs["key_filename"] = str(key_path)
            else:
                return False, f"SSH key not found: {key_file}", 0
        elif password:
            connect_kwargs["password"] = password
        else:
            return False, "No valid authentication method configured", 0

        client.connect(**connect_kwargs)

        stdin, stdout, stderr = client.exec_command(command, timeout=60)
        exit_code = stdout.channel.recv_exit_status()

        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")

        client.close()

        duration = (datetime.now() - start_time).total_seconds()

        if exit_code != 0:
            return False, f"Exit code {exit_code}:\n{error or output}", duration

        return True, output, duration

    except paramiko.AuthenticationException as e:
        duration = (datetime.now() - start_time).total_seconds()
        logger.error(f"SSH auth failed for {host.get('hostname')}: {e}")
        return False, f"Authentication failed", duration

    except paramiko.SSHException as e:
        duration = (datetime.now() - start_time).total_seconds()
        logger.error(f"SSH error for {host.get('hostname')}: {e}")
        return False, f"SSH error: {e}", duration

    except Exception as e:
        duration = (datetime.now() - start_time).total_seconds()
        logger.error(f"Error executing on {host.get('hostname')}: {e}")
        return False, str(e), duration

# =============================================================================
# Flask Routes
# =============================================================================

@app.before_request
def check_auth():
    """Verify API token for all requests except health check."""
    if request.endpoint == "health":
        return None

    if not API_TOKEN:
        logger.error("SSH_PROXY_TOKEN not configured!")
        return jsonify({"error": "Server misconfigured - no API token"}), 500

    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    if token != API_TOKEN:
        client_ip = request.remote_addr
        logger.warning(f"Unauthorized request from {client_ip}")
        return jsonify({"error": "Unauthorized"}), 401


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint (no auth required)."""
    return jsonify({
        "status": "ok",
        "hosts_configured": len(ALLOWED_HOSTS),
        "commands_configured": len(ALLOWED_COMMANDS)
    })


@app.route("/hosts", methods=["GET"])
def list_hosts():
    """List available hosts (names only, no sensitive info)."""
    hosts = [{"name": h.get("name")} for h in ALLOWED_HOSTS]
    return jsonify({"hosts": hosts})


@app.route("/commands", methods=["GET"])
def list_commands():
    """List allowed command patterns with descriptions."""
    commands = [
        {"pattern": c.get("pattern"), "description": c.get("description", "")}
        for c in ALLOWED_COMMANDS
    ]
    return jsonify({"commands": commands})


@app.route("/execute", methods=["POST"])
def execute():
    """
    Execute a command on a host.

    Request body:
    {
        "host": "hostname",
        "command": "command to execute"
    }
    """
    data = request.get_json()

    if not data:
        return jsonify({"error": "Invalid JSON body"}), 400

    host_name = data.get("host", "").strip()
    command = data.get("command", "").strip()

    if not host_name:
        return jsonify({"error": "Missing 'host' parameter"}), 400

    if not command:
        return jsonify({"error": "Missing 'command' parameter"}), 400

    # Validate host
    host = get_host(host_name)
    if not host:
        logger.warning(f"Request for unknown host: {host_name}")
        return jsonify({
            "error": f"Host '{host_name}' not in allowed list",
            "available_hosts": [h.get("name") for h in ALLOWED_HOSTS]
        }), 403

    # Validate command
    allowed, description = is_command_allowed(command)
    if not allowed:
        logger.warning(f"Blocked command on {host_name}: {command}")
        return jsonify({
            "error": "Command not in allowed list",
            "command": command,
            "hint": "Use GET /commands to see allowed patterns"
        }), 403

    # Log the execution
    client_ip = request.remote_addr
    logger.info(f"EXEC [{client_ip}] {host_name}: {command}")

    # Execute
    success, output, duration = execute_ssh(host, command)

    # Log result
    status = "OK" if success else "FAIL"
    logger.info(f"RESULT [{status}] {host_name} ({duration:.2f}s)")

    return jsonify({
        "success": success,
        "host": host_name,
        "command": command,
        "description": description,
        "output": output,
        "duration_seconds": round(duration, 2)
    })


@app.route("/test-connection", methods=["POST"])
def test_connection():
    """Test SSH connection to a host without executing a command."""
    data = request.get_json() or {}
    host_name = data.get("host", "").strip()

    if not host_name:
        return jsonify({"error": "Missing 'host' parameter"}), 400

    host = get_host(host_name)
    if not host:
        return jsonify({"error": f"Host '{host_name}' not configured"}), 404

    try:
        client = paramiko.SSHClient()

        # AutoAddPolicy: Accept all host keys for internal infrastructure
        # Security is enforced via hosts.yaml allowlist, not host key verification
        # (Hashed known_hosts makes matching difficult, and this is internal/private network)
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        key_file = host.get("key_file")
        password = host.get("password")

        connect_kwargs = {
            "hostname": host["hostname"],
            "username": host["username"],
            "port": host.get("port", 22),
            "timeout": 10,
        }

        if key_file and Path(key_file).exists():
            connect_kwargs["key_filename"] = key_file
        elif password:
            connect_kwargs["password"] = password

        client.connect(**connect_kwargs)
        client.close()

        return jsonify({
            "success": True,
            "host": host_name,
            "message": "Connection successful"
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "host": host_name,
            "error": str(e)
        })


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    if not API_TOKEN:
        logger.error("SSH_PROXY_TOKEN environment variable not set!")
        logger.error("Set it in .env file or environment")
        sys.exit(1)

    if not ALLOWED_HOSTS:
        logger.warning("No hosts configured! Copy hosts.yaml.example to hosts.yaml")

    logger.info(f"Starting SSH Proxy on port {LISTEN_PORT}")
    app.run(host="0.0.0.0", port=LISTEN_PORT, debug=DEBUG)

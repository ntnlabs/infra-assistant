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
- SSH_PROXY_URL, SSH_PROXY_TOKEN
- SLURM_MASTER_HOST, SLURM_WRAPPER_COMMAND
- POLL_INTERVAL, CONVERSATION_TIMEOUT, DEBUG

Usage:
    python bot.py
"""

import os
import re
import sys
import time
import signal
import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from rocketchat_API.rocketchat import RocketChat

import audit
import reminders

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

# SSH Proxy (for command execution - independent gatekeeper)
SSH_PROXY_URL = os.environ.get("SSH_PROXY_URL", "http://localhost:5001")
SSH_PROXY_TOKEN = os.environ.get("SSH_PROXY_TOKEN", "")

# Slurm (optional, via ssh-proxy to Slurm master wrapper)
SLURM_MASTER_HOST = os.environ.get("SLURM_MASTER_HOST", "").strip()
SLURM_WRAPPER_COMMAND = os.environ.get("SLURM_WRAPPER_COMMAND", "/usr/local/bin/bob-slurm").strip() or "/usr/local/bin/bob-slurm"
SLURM_DEFAULT_PARTITION = os.environ.get("SLURM_DEFAULT_PARTITION", "").strip()

# Settings
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "2"))
CONVERSATION_TIMEOUT = int(os.environ.get("CONVERSATION_TIMEOUT", "3600"))
DM_REFRESH_INTERVAL = int(os.environ.get("DM_REFRESH_INTERVAL", "60"))
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

# Ollama model settings
OLLAMA_TEMPERATURE = float(os.environ.get("OLLAMA_TEMPERATURE", "0.2"))
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))
MAX_OLLAMA_ITERATIONS = min(int(os.environ.get("MAX_OLLAMA_ITERATIONS", "5")), 25)  # Hard cap at 25
MAX_OLLAMA_CONCURRENCY = max(1, min(int(os.environ.get("MAX_OLLAMA_CONCURRENCY", "3")), 10))  # Range: 1-10

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
MAX_CONVERSATIONS = 500  # Prevent unbounded memory growth

# Track processed message IDs to avoid duplicates (OrderedDict for FIFO cleanup)
processed_messages: OrderedDict = OrderedDict()
MAX_PROCESSED_MESSAGES = 10000

# Threading: Limit concurrent Ollama calls (VRAM constraint)
# Semaphore initialized after settings loaded
ollama_semaphore = None  # Will be set in main()
conversations_lock = threading.Lock()  # Thread-safe conversation access
processed_messages_lock = threading.Lock()  # Thread-safe processed messages
shutdown_event = threading.Event()  # Graceful shutdown signal

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

1. **query_audit_log** - Search the persistent audit log of past actions taken by this bot
   - Use when: Users ask "when did you...", "did you drain...", "what did you do to...", "why did you...", "show me history of..."
   - Can filter by: tool name, node/host name, username, time window
   - Returns: Timestamped log of tool executions including the user request that triggered them

2. **get_active_alerts** - Retrieves current problems from Zabbix monitoring
   - Use when: Users ask about alerts, problems, issues, or "what's wrong"
   - Returns: List of active alerts with severity and affected hosts
   - Can filter by severity level (0-5)

3. **get_infrastructure_summary** - Gets overview of monitored infrastructure
   - Use when: Users ask about overall status, host counts, or general health
   - Returns: Total hosts, hosts up/down, active problems, and trigger counts

4. **manage_alert** - Manage Zabbix alerts
   - Use when: Users ask to acknowledge, close, change severity, or suppress/postpone an alert
   - Requires: Event ID (from get_active_alerts)
   - ONLY valid actions (do NOT invent other action names):
     - acknowledge: Mark alert as seen
     - close: Resolve the alert
     - change_severity: Change alert severity (0=Not classified, 1=Info, 2=Warning, 3=Average, 4=High, 5=Disaster)
     - suppress: Temporarily suppress/postpone/snooze alert notifications
   - To postpone/snooze: use action=suppress with suppress_days=30 (for 30 days), NOT action=postpone
   - Can combine: use action=acknowledge + suppress_days=30 to both acknowledge AND postpone in one call
   - Can add optional message/comment explaining the action taken

5. **run_command** - Execute diagnostic commands on remote hosts via SSH
   - Use when: Users ask to check disk space, memory, CPU, uptime on specific servers
   - Available commands: df/disk (disk space), memory/free/ram, uptime, load, cpu, processes, network, listening
   - Requires: Hostname from allowed list
   - Returns: Command output from the remote host

6. **get_slurm_nodes** - Get Slurm cluster/node summary from Slurm master
   - Use when: Users ask about node availability, idle/allocated/drained nodes, partition health
   - Optional: Partition filter

7. **manage_slurm_node** - Check, drain, or resume a Slurm node (via audited wrapper)
   - Actions:
     - check: Get status for one node
     - drain: Put node into DRAIN state (requires reason and explicit confirmation)
     - resume: Resume a drained node (requires explicit confirmation)
   - Safety: Mutating actions require `confirm=true`
   - Requires: Node name

7a. **get_slurm_jobs** - List currently queued/running Slurm jobs
   - Use when: Users ask about active jobs, running jobs, job queue
   - Optional filters: `user` (username), `node` (hostname — e.g. ci02.tsk.example.com)
   - **Always pass `node` when the user mentions a specific node**

7b. **get_slurm_job_details** - Get full details for one job by ID
   - Use when: Users ask about a specific job ID

7c. **get_slurm_job_history** - Get completed/failed job history via sacct
   - Use when: Users ask about past, finished, or failed jobs

8. **set_reminder** - Set a reminder to fire at a specific UTC time
   - Use when: Users ask to be reminded about something ("remind me to...", "set a reminder for...")
   - Params: `message` (what to remind), `fire_at` (ISO UTC datetime), `recurrence_minutes` (0 = one-shot, >0 = repeating interval in minutes)
   - Use the current date/time (shown above in system context) to convert relative times ("in 2 minutes", "tomorrow at 9am", "every Monday at 10am") to absolute UTC
   - Always confirm the scheduled UTC time back to the user when creating a reminder
   - fire_at MUST be in the future — reject past times
   - Examples: "every day at 9am" → recurrence_minutes=1440, "every week" → recurrence_minutes=10080

9. **list_reminders** - List all pending reminders in this room
   - Use when: Users ask "what reminders are set?" or "show my reminders"
   - No parameters needed

10. **delete_reminder** - Delete a reminder by ID
    - Use when: Users ask to cancel or delete a reminder ("delete reminder 3")
    - Params: `reminder_id` (integer)
    - If the user does not provide a reminder_id, check conversation history for a
      recently fired reminder. If still unclear, call list_reminders and ask which one.

11. **snooze_reminder** - Postpone a reminder by N minutes
    - Use when: Users say "snooze reminder N for X minutes/hours"
    - Params: `reminder_id` (integer), `snooze_minutes` (integer, must be > 0)
    - If the user does not provide a reminder_id, check conversation history for a
      recently fired reminder. If still unclear, call list_reminders and ask which one.

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

## CRITICAL RULE: NEVER INVENT OR GUESS INFORMATION

This is your most important rule. ALWAYS follow it:

❌ NEVER DO THIS:
- Don't invent alert IDs (like "12345", "67890")
- Don't guess hostnames or make up server names
- Don't create plausible-sounding data when tools fail
- Don't fill in missing information with assumptions
- Don't say things are "fine" or "normal" without checking tools
- **NEVER claim you acknowledged/closed/suppressed an alert unless you received a tool result confirming it**
- **NEVER output raw JSON or tool call syntax in your chat messages** - always respond in plain natural language
- **NEVER invent tool names** like "postpone_events" - only use the tools listed above

✅ ALWAYS DO THIS INSTEAD:
- If a tool fails: Say "I cannot access Zabbix right now" or "The command failed"
- If data is missing: Say "I don't have that information"
- If you're unsure: Ask the user for clarification
- If tools return errors: Report the exact error to the user
- When you don't know: Say "I don't know" - this is professional and correct
- After calling manage_alert: report the tool result in plain language (e.g. "Done, acknowledged alert 4382836")

EXAMPLES OF CORRECT BEHAVIOR:
- User asks about alerts, Zabbix is down → "I'm unable to connect to Zabbix right now. Please check if the monitoring system is accessible."
- User asks about a host you can't find → "I don't see that hostname in the monitoring system. Could you verify the name?"
- Tool returns empty results → "There are currently no alerts matching those criteria" (NOT "Everything looks good!")

## Important Behaviors

- **Always check tools first** before saying you don't know about current status
- **Report tool failures clearly** - If Zabbix is down or a command fails, tell the user explicitly
- **Ask clarifying questions** if a request is ambiguous
- **Require explicit confirmation** for mutating Slurm operations (drain/resume)
- **Acknowledge limitations** - you can monitor and recommend, but humans make final decisions
- **Maintain context** - remember what was discussed earlier in the conversation
- **Be proactive** - if you see critical alerts, mention them even if not directly asked

## CRITICAL: Present Tool Results Directly

When a tool returns data, YOU MUST present it to the user:
- ❌ DON'T say "You can use the manage_alert tool" or describe HOW to use tools
- ❌ DON'T summarize or paraphrase - show the actual data
- ❌ DON'T say "Here's a summary" - SHOW THE COMPLETE DATA
- ❌ DON'T truncate or condense long lists - users need ALL the data
- ✅ DO present the full tool result data to the user
- ✅ DO show complete JSON output when tools return JSON
- ✅ DO copy the entire data block even if it's 50+ lines
- ✅ DO format it clearly (use the data as-is or improve formatting)
- ✅ DO add context or analysis AFTER presenting the data

**ESPECIALLY FOR STRUCTURED DATA (JSON, lists, tables):**
- If a tool returns 60 jobs, show ALL 60 jobs - do not summarize to "15 running, 45 pending"
- If a tool returns JSON, copy the ENTIRE JSON output - users need the complete data
- NEVER say "I can show you details if needed" - SHOW IT IMMEDIATELY

Example:
- User: "show me active alerts"
- Tool returns: "Found 2 alerts: [High] Database down on db01, [Warning] Disk space low on web02"
- ✅ CORRECT: Present that exact data to the user
- ❌ WRONG: "You can use manage_alert to handle these issues"

## CRITICAL: Never Hallucinate

If a tool returns an error or no data:
- ❌ DON'T: Make up alert IDs like "12345" or "67890"
- ❌ DON'T: Invent plausible-sounding hostnames or problems
- ✅ DO: Say "I'm unable to connect to Zabbix right now" or "The command failed with error: [error message]"
- ✅ DO: Suggest checking if the service is running or accessible

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

def get_active_alerts(min_severity: int = 0, limit: int = 25, host: str = "") -> dict:
    """Get active alerts from Zabbix, optionally filtered by hostname.

    Args:
        min_severity: Minimum severity (0-5). Default 0 (all severities).
        limit: Max number of alerts to return.
        host: Optional hostname to filter alerts for a specific host.

    Returns:
        dict with 'success' and 'data' or 'error'
    """
    severity_names = ["Not classified", "Information", "Warning", "Average", "High", "Disaster"]

    try:
        hostname = host.strip() if host else ""

        if hostname:
            # Use host-specific endpoint
            response = requests.get(
                f"{ZABBIX_PROXY_URL}/host/{hostname}/problems",
                headers={"Authorization": f"Bearer {ZABBIX_PROXY_TOKEN}"},
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            problems = data.get("problems", [])

            if not problems:
                return {"success": True, "data": f"No active alerts found for host '{hostname}'."}

            result = f"Found {len(problems)} active alerts for host '{hostname}':\n\n"
            for p in problems[:limit]:
                severity = p.get("severity", 0)
                sev_name = severity_names[severity] if 0 <= severity <= 5 else "Unknown"
                eventid = p.get('eventid', 'unknown')
                ack = " [ACK]" if p.get("acknowledged") else ""
                result += f"[{sev_name}]{ack} {p.get('name', 'Unknown')} (ID: {eventid})\n"
        else:
            # All hosts
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

            result = f"Found {len(problems)} active alerts:\n\n"
            for p in problems[:limit]:
                severity = p.get("severity", 0)
                sev_name = severity_names[severity] if 0 <= severity <= 5 else "Unknown"
                eventid = p.get('eventid', 'unknown')
                result += f"[{sev_name}] {p.get('name', 'Unknown')} on {p.get('hostname', 'Unknown')} (ID: {eventid})\n"

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

        # Note: Proxy returns flat structure, not nested "summary"
        result = f"""Infrastructure Summary:
- Total Problems: {data.get('total_problems', 0)}
- By Severity: {', '.join(f"{k}={v}" for k, v in data.get('by_severity', {}).items())}
- High Severity Issues: {len(data.get('high_severity_problems', []))}"""

        return {"success": True, "data": result}

    except Exception as e:
        logger.error(f"Error getting summary: {e}")
        return {"success": False, "error": str(e)}


def manage_alert(event_id: str, action: str = "acknowledge", message: str = "", severity: int = None, suppress_hours: int = None, suppress_days: int = None) -> dict:
    """Manage a Zabbix alert - acknowledge, close, change severity, or suppress.

    Args:
        event_id: Event ID from Zabbix (get from active alerts)
        action: Action to perform - "acknowledge", "close", "change_severity", "suppress"
        message: Optional comment/message to add
        severity: New severity (0-5) for change_severity action
        suppress_hours: Hours to suppress alert. Converted from suppress_days if provided.
        suppress_days: Days to suppress alert (easier than hours). 30 days = suppress_days=30.

    Returns:
        dict with 'success' and 'data' or 'error'
    """
    # Normalize action aliases
    action_aliases = {"postpone": "suppress", "snooze": "suppress", "delay": "suppress", "mute": "suppress"}
    action = action_aliases.get(action.lower(), action.lower())

    # Convert suppress_days to hours if provided
    if suppress_days and not suppress_hours:
        suppress_hours = int(suppress_days) * 24

    # Validate suppress duration is positive
    if suppress_hours is not None and int(suppress_hours) <= 0:
        return {"success": False, "error": "suppress_hours must be a positive number"}

    # Suppress requires a duration — reject if none provided
    if action == "suppress" and not suppress_hours:
        return {"success": False, "error": "suppress requires a duration (suppress_hours or suppress_days). Ask the user how long to suppress the alert."}

    # If a suppress duration is given with any non-suppress action, always also acknowledge.
    # This way it doesn't matter if the model picks action=suppress or action=acknowledge —
    # both acknowledge AND suppress will happen as long as suppress_days/suppress_hours is set.
    if suppress_hours and action == "suppress":
        action = "acknowledge"  # proxy will then make two calls: acknowledge + suppress

    # Validate event_id format (must be numeric)
    if not event_id or not re.fullmatch(r"\d+", str(event_id)):
        return {"success": False, "error": "Invalid event_id format (must be numeric)"}

    try:
        payload = {
            "event_ids": [event_id],
            "action": action,
            "message": message
        }

        if action == "change_severity":
            if severity is None:
                return {"success": False, "error": "Severity level (0-5) required for change_severity"}
            payload["severity"] = severity

        if suppress_hours:
            suppress_until = int(time.time()) + (suppress_hours * 3600)
            payload["suppress_until"] = suppress_until

        response = requests.post(
            f"{ZABBIX_PROXY_URL}/acknowledge",
            headers={
                "Authorization": f"Bearer {ZABBIX_PROXY_TOKEN}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=30
        )
        response.raise_for_status()
        data = response.json()

        if data.get("success"):
            if action == "change_severity":
                severity_names = ["Not classified", "Information", "Warning", "Average", "High", "Disaster"]
                sev_name = severity_names[severity] if 0 <= severity <= 5 else str(severity)
                result = f"✅ Changed severity of alert {event_id} to {sev_name}"
            elif action == "suppress":
                hours = suppress_hours or "indefinitely"
                result = f"✅ Suppressed alert {event_id} for {hours} hours"
            else:
                result = f"✅ Successfully {action}d alert {event_id}"

            if suppress_hours and action != "suppress":
                result += f" and suppressed for {suppress_hours} hours"
            if message:
                result += f" with message: '{message}'"
            return {"success": True, "data": result}
        else:
            return {"success": False, "error": data.get("error", "Unknown error")}

    except Exception as e:
        logger.error(f"Error managing alert: {e}")
        return {"success": False, "error": str(e)}


def get_help() -> dict:
    """Get help information about what Bob can do.

    Returns:
        dict with 'success' and 'data' containing help text
    """
    help_text = """# Bob - Infrastructure Assistant Help

## Available Commands

### Monitoring & Alerts
- **Show alerts**: "@bob show active alerts" or "@bob what's wrong?"
- **Infrastructure status**: "@bob infrastructure summary" or "@bob status"
- **Acknowledge alert**: "@bob acknowledge alert 12345"
- **Close alert**: "@bob close event 12345 fixed by restart"
- **Change severity**: "@bob change severity of alert 12345 to warning"
- **Suppress/postpone**: "@bob suppress alert 12345 for 2 hours"

### SSH Commands
You can run these commands on allowed hosts:
"""

    # Add available SSH commands
    ssh_commands = {
        "df/disk/disk_space": "Check disk usage",
        "memory/free/ram": "Check memory usage",
        "uptime": "Show system uptime",
        "load": "Show load average",
        "cpu": "Show CPU usage",
        "processes": "Show top processes",
        "network": "Show network interfaces",
        "listening": "Show listening ports"
    }

    for cmd, desc in ssh_commands.items():
        help_text += f"- **{cmd}**: {desc}\n"

    help_text += "\n**Example**: \"@bob check disk on web01\"\n\n"

    # Note about SSH commands
    help_text += """### SSH Commands
SSH commands are validated by an independent SSH Proxy.
Allowed hosts and commands are configured in:
- `ssh-proxy/hosts.yaml` (allowed hosts)
- `ssh-proxy/commands.yaml` (allowed commands)

"""

    help_text += """### Slurm Commands
- **Slurm summary**: "@bob show slurm nodes" or "@bob slurm summary"
- **Slurm node status**: "@bob check slurm node gpu001"
- **Drain node**: "@bob drain slurm node gpu001 reason maintenance ticket-123 confirm"
- **Resume node**: "@bob resume slurm node gpu001 confirm"
- **Job queue**: "@bob show slurm jobs" or "@bob slurm queue"
- **Job queue for user**: "@bob show jobs for user john"
- **Job details**: "@bob show details for job 12345"
- **Job history**: "@bob show failed jobs" or "@bob show completed jobs from last 48 hours"
- **User job history**: "@bob show john's jobs from last 24 hours"

Safety:
- Drain/resume require explicit confirmation (`confirm=true` in tool call)
- Slurm actions run through a restricted wrapper on the Slurm master
"""

    help_text += """
## Usage Tips
- Mention @bob anywhere in your message (doesn't have to be at start)
- Works in channels and DMs
- Ask naturally: "bob, what's the disk space on web01?"
- Use event IDs from alerts to acknowledge/close them

## Examples
```
@bob show me high severity alerts
@bob what's the infrastructure status?
@bob check memory on db01
@bob acknowledge alert 12345
hey @bob, can you check disk space on web02?
```
"""

    # Check for custom help file
    help_file = Path(__file__).parent.parent / "HELP.md"
    if help_file.exists():
        try:
            with open(help_file, 'r') as f:
                custom_help = f.read()
            help_text += "\n\n## Additional Information\n\n" + custom_help
        except Exception as e:
            logger.debug(f"Could not read HELP.md: {e}")

    return {"success": True, "data": help_text}


def run_command(host: str, command: str) -> dict:
    """Request command execution via SSH Proxy (independent gatekeeper).

    IMPORTANT: This function does NOT execute commands directly.
    It sends a request to the SSH Proxy service, which:
    - Validates host against its own hosts.yaml allowlist
    - Validates command against its own commands.yaml patterns
    - Executes independently (Bob cannot bypass validation)

    Args:
        host: Hostname (ssh-proxy validates against hosts.yaml)
        command: Command shorthand (ssh-proxy validates against commands.yaml)

    Returns:
        dict with 'success' and 'data' or 'error'
    """
    if not host:
        return {"success": False, "error": "host is required"}
    if not command:
        return {"success": False, "error": "command is required"}

    # Map common command names to actual commands
    # Note: ssh-proxy will do final validation against commands.yaml
    COMMAND_MAP = {
        "df": "df -h",
        "disk": "df -h",
        "disk_space": "df -h",
        "memory": "free -h",
        "free": "free -h",
        "ram": "free -h",
        "uptime": "uptime",
        "load": "cat /proc/loadavg",
        "cpu": "top -bn1 | head -20",
        "processes": "ps aux --sort=-%mem | head -15",
        "network": "ip addr show",
        "listening": "ss -tlnp",
    }

    # Map shorthand to actual command
    actual_command = COMMAND_MAP.get(command.lower(), command)

    proxy_result = _execute_via_ssh_proxy(host=host, command=actual_command, timeout=60)
    if not proxy_result.get("success"):
        return proxy_result

    return {"success": True, "data": f"Output from {host}:\n```\n{proxy_result.get('output', '')}\n```"}


def _execute_via_ssh_proxy(host: str, command: str, timeout: int = 60) -> dict:
    """Execute one command through ssh-proxy and return raw output/error."""
    if not SSH_PROXY_URL:
        return {"success": False, "error": "SSH_PROXY_URL not configured"}

    try:
        logger.info(f"Requesting ssh-proxy to run '{command}' on {host}")
        response = requests.post(
            f"{SSH_PROXY_URL}/execute",
            headers={"Authorization": f"Bearer {SSH_PROXY_TOKEN}"},
            json={"host": host, "command": command},
            timeout=timeout
        )

        if response.status_code == 401:
            return {"success": False, "error": "SSH Proxy authentication failed (check SSH_PROXY_TOKEN)"}

        if response.status_code == 403:
            data = response.json()
            return {"success": False, "error": f"SSH Proxy rejected: {data.get('error', 'Forbidden')}"}

        response.raise_for_status()
        data = response.json()

        if data.get("success"):
            return {"success": True, "output": data.get("output", ""), "description": data.get("description", "")}

        return {"success": False, "error": data.get("output") or data.get("error", "Unknown error from ssh-proxy")}

    except requests.exceptions.Timeout:
        return {"success": False, "error": "SSH Proxy request timed out"}
    except requests.exceptions.ConnectionError:
        return {"success": False, "error": "Cannot connect to SSH Proxy service (is it running?)"}
    except Exception as e:
        logger.error(f"Error calling SSH Proxy: {e}")
        return {"success": False, "error": str(e)}


def get_slurm_nodes(partition: str = "") -> dict:
    """Get Slurm node summary from Slurm master via wrapper script."""
    if not SLURM_MASTER_HOST:
        return {"success": False, "error": "SLURM_MASTER_HOST not configured"}

    part = (partition or SLURM_DEFAULT_PARTITION).strip()
    if part and not re.fullmatch(r"[A-Za-z0-9_.-]+", part):
        return {"success": False, "error": "Invalid partition name format"}

    cmd = f"{SLURM_WRAPPER_COMMAND} summary"
    if part:
        cmd += f" --partition {part}"

    proxy_result = _execute_via_ssh_proxy(host=SLURM_MASTER_HOST, command=cmd, timeout=60)
    if not proxy_result.get("success"):
        return proxy_result

    output = proxy_result.get("output", "").strip()
    return {"success": True, "data": f"Slurm summary from {SLURM_MASTER_HOST}:\n```json\n{output}\n```"}


def manage_slurm_node(action: str, node: str, reason: str = "", confirm: bool = False) -> dict:
    """Check, drain, or resume a Slurm node using the slurm wrapper."""
    if not SLURM_MASTER_HOST:
        return {"success": False, "error": "SLURM_MASTER_HOST not configured"}

    action_normalized = (action or "").strip().lower()
    if action_normalized not in {"check", "drain", "resume"}:
        return {"success": False, "error": "Invalid action. Use: check, drain, resume"}

    if isinstance(confirm, str):
        confirm = confirm.strip().lower() in {"1", "true", "yes", "confirm"}
    else:
        confirm = bool(confirm)

    node_name = (node or "").strip()
    if not node_name:
        return {"success": False, "error": "Node is required"}
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", node_name):
        return {"success": False, "error": "Invalid node name format"}

    # Mutating actions require explicit confirmation.
    if action_normalized in {"drain", "resume"} and not confirm:
        return {"success": False, "error": f"Action '{action_normalized}' requires explicit confirmation (confirm=true)"}

    if action_normalized == "check":
        cmd = f"{SLURM_WRAPPER_COMMAND} node-status --node {node_name}"
    elif action_normalized == "resume":
        cmd = f"{SLURM_WRAPPER_COMMAND} resume --node {node_name}"
    else:
        reason_text = (reason or "").strip()
        if not reason_text:
            return {"success": False, "error": "Drain requires a reason"}
        if len(reason_text) > 120:
            return {"success": False, "error": "Reason too long (max 120 chars)"}
        if not re.fullmatch(r"[A-Za-z0-9 ._:@/#-]+", reason_text):
            return {"success": False, "error": "Reason contains unsupported characters"}
        # Always quote reason for pattern matching (validated above to prevent injection)
        cmd = f"{SLURM_WRAPPER_COMMAND} drain --node {node_name} --reason '{reason_text}'"

    proxy_result = _execute_via_ssh_proxy(host=SLURM_MASTER_HOST, command=cmd, timeout=90)
    if not proxy_result.get("success"):
        return proxy_result

    output = proxy_result.get("output", "").strip()
    return {"success": True, "data": f"Slurm {action_normalized} result for {node_name}:\n```json\n{output}\n```"}


def get_slurm_jobs(user: str = "", node: str = "") -> dict:
    """Get Slurm job queue, optionally filtered by username and/or node."""
    if not SLURM_MASTER_HOST:
        return {"success": False, "error": "SLURM_MASTER_HOST not configured"}

    username = (user or "").strip()
    if username and not re.fullmatch(r"[a-zA-Z0-9_-]+", username):
        return {"success": False, "error": "Invalid username format"}

    node_name = (node or "").strip()
    if node_name and not re.fullmatch(r"[A-Za-z0-9_.:-]+", node_name):
        return {"success": False, "error": "Invalid node format"}

    cmd = f"{SLURM_WRAPPER_COMMAND} queue"
    if username:
        cmd += f" --user {username}"
    if node_name:
        cmd += f" --node {node_name}"

    proxy_result = _execute_via_ssh_proxy(host=SLURM_MASTER_HOST, command=cmd, timeout=60)
    if not proxy_result.get("success"):
        return proxy_result

    output = proxy_result.get("output", "").strip()
    return {"success": True, "data": f"Slurm job queue from {SLURM_MASTER_HOST}:\n```json\n{output}\n```"}


def get_slurm_job_details(jobid: str) -> dict:
    """Get detailed information about a specific Slurm job."""
    if not SLURM_MASTER_HOST:
        return {"success": False, "error": "SLURM_MASTER_HOST not configured"}

    job_id = (jobid or "").strip()
    if not job_id:
        return {"success": False, "error": "Job ID is required"}
    if not re.fullmatch(r"[0-9]+", job_id):
        return {"success": False, "error": "Invalid job ID format (must be numeric)"}

    cmd = f"{SLURM_WRAPPER_COMMAND} job --jobid {job_id}"

    proxy_result = _execute_via_ssh_proxy(host=SLURM_MASTER_HOST, command=cmd, timeout=60)
    if not proxy_result.get("success"):
        return proxy_result

    output = proxy_result.get("output", "").strip()
    return {"success": True, "data": f"Slurm job {job_id} details from {SLURM_MASTER_HOST}:\n```json\n{output}\n```"}


def get_slurm_job_history(user: str = "", state: str = "", hours: int = 24, limit: int = 50) -> dict:
    """Get Slurm job history using sacct. Can filter by user, state, time range, and limit results."""
    if not SLURM_MASTER_HOST:
        return {"success": False, "error": "SLURM_MASTER_HOST not configured"}

    username = (user or "").strip()
    if username and not re.fullmatch(r"[a-zA-Z0-9_-]+", username):
        return {"success": False, "error": "Invalid username format"}

    job_state = (state or "").strip().upper()
    if job_state and not re.fullmatch(r"[A-Z_]+", job_state):
        return {"success": False, "error": "Invalid state format"}

    try:
        hours_int = int(hours)
        limit_int = int(limit)
    except (ValueError, TypeError):
        return {"success": False, "error": "Hours and limit must be numeric"}

    if hours_int <= 0 or hours_int > 720:  # Max 30 days
        return {"success": False, "error": "Hours must be between 1 and 720"}
    if limit_int <= 0 or limit_int > 500:
        return {"success": False, "error": "Limit must be between 1 and 500"}

    cmd = f"{SLURM_WRAPPER_COMMAND} history"
    if username:
        cmd += f" --user {username}"
    if job_state:
        cmd += f" --state {job_state}"
    cmd += f" --hours {hours_int} --limit {limit_int}"

    proxy_result = _execute_via_ssh_proxy(host=SLURM_MASTER_HOST, command=cmd, timeout=90)
    if not proxy_result.get("success"):
        return proxy_result

    output = proxy_result.get("output", "").strip()
    return {"success": True, "data": f"Slurm job history from {SLURM_MASTER_HOST}:\n```json\n{output}\n```"}


# Tool definitions for LLM
# Tool definitions in Ollama format (OpenAI-compatible)
OLLAMA_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_audit_log",
            "description": "Search the persistent audit log of past actions taken by this bot. Use when users ask 'when did you drain gpu001?', 'did you acknowledge that alert?', 'what did you do to host X?', 'show history for node Y', 'why was this drained?'. All filters are optional.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Filter by tool: manage_alert, manage_slurm_node, run_command, etc. Leave empty for all."
                    },
                    "node": {
                        "type": "string",
                        "description": "Filter by node or host name in the tool arguments, e.g. 'gpu001' or 'ex1.tca'."
                    },
                    "user": {
                        "type": "string",
                        "description": "Filter by Rocket.Chat username who requested the action."
                    },
                    "hours": {
                        "type": "integer",
                        "description": "How many hours to look back. Default 168 (7 days).",
                        "default": 168
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return. Default 20.",
                        "default": 20
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_active_alerts",
            "description": "Get current active alerts from Zabbix monitoring system. Use this when users ask about problems, alerts, or issues. IMPORTANT: Use min_severity=0 by default to show ALL alerts unless the user explicitly asks to filter by severity (e.g. 'only high alerts').",
            "parameters": {
                "type": "object",
                "properties": {
                    "min_severity": {
                        "type": "integer",
                        "description": "Minimum severity level. Default 0 = ALL severities. Only increase this if user explicitly asks to filter (e.g. 'high alerts only' = 4). Values: 0=All, 1=Info, 2=Warning, 3=Average, 4=High, 5=Disaster",
                        "default": 0
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of alerts to return",
                        "default": 25
                    },
                    "host": {
                        "type": "string",
                        "description": "Optional: filter alerts for a specific hostname (e.g. 'ex1.tca'). Leave empty for all hosts."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_infrastructure_summary",
            "description": "Get overview of infrastructure status including host counts and active problems. Use this for general status questions.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_alert",
            "description": "Manage Zabbix alerts - acknowledge, close, change severity, or suppress. Use when users ask to manage alerts. Requires event ID from active alerts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "Event ID from Zabbix (get from active alerts first)"
                    },
                    "action": {
                        "type": "string",
                        "description": "ONLY valid values: 'acknowledge' (mark as seen), 'close' (resolve), 'change_severity', 'suppress' (postpone/snooze). Do NOT use 'postpone' or any other value.",
                        "enum": ["acknowledge", "close", "change_severity", "suppress"],
                        "default": "acknowledge"
                    },
                    "message": {
                        "type": "string",
                        "description": "Optional comment/message to add"
                    },
                    "severity": {
                        "type": "integer",
                        "description": "New severity for change_severity action (0-5)"
                    },
                    "suppress_days": {
                        "type": "integer",
                        "description": "Number of DAYS to suppress/postpone the alert. Use this when user says 'postpone for X days'. Example: 30 days = suppress_days=30. Can be combined with any action."
                    },
                    "suppress_hours": {
                        "type": "integer",
                        "description": "Number of HOURS to suppress/postpone the alert. Use suppress_days instead when user specifies days. Can be combined with any action."
                    }
                },
                "required": ["event_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run diagnostic command on remote host via SSH. Use when users ask to check disk, memory, uptime, load, CPU, or run commands on servers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Hostname or IP address (must be in allowed list)"
                    },
                    "command": {
                        "type": "string",
                        "description": "Command to run: df, disk, memory, uptime, load, cpu, processes, network, listening"
                    }
                },
                "required": ["host", "command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_slurm_nodes",
            "description": "Get Slurm node/cluster summary from Slurm master. Use this for Slurm availability/status questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "partition": {
                        "type": "string",
                        "description": "Optional Slurm partition name filter"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_slurm_node",
            "description": "Check, drain, or resume a Slurm node. Drain/resume are mutating and require confirm=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["check", "drain", "resume"],
                        "description": "Node management action"
                    },
                    "node": {
                        "type": "string",
                        "description": "Slurm node name"
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason text for drain action"
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "Must be true for drain/resume",
                        "default": False
                    }
                },
                "required": ["action", "node"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_slurm_jobs",
            "description": "Get Slurm job queue, optionally filtered by username and/or node. Use when users ask about running jobs, queued jobs, or job status. If a node name is mentioned, always pass it as the node parameter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user": {
                        "type": "string",
                        "description": "Optional: Filter jobs by username"
                    },
                    "node": {
                        "type": "string",
                        "description": "Optional: Filter jobs by node hostname (e.g. ci02.tsk.example.com)"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_slurm_job_details",
            "description": "Get detailed information about a specific Slurm job by job ID. Use when users ask for details about a particular job.",
            "parameters": {
                "type": "object",
                "properties": {
                    "jobid": {
                        "type": "string",
                        "description": "Slurm job ID (numeric)"
                    }
                },
                "required": ["jobid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_slurm_job_history",
            "description": "Get Slurm job history (completed, failed, cancelled jobs) using sacct. Use when users ask about past jobs, failed jobs, or job history. Common states: COMPLETED, FAILED, CANCELLED, TIMEOUT, NODE_FAIL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user": {
                        "type": "string",
                        "description": "Optional: Filter by username"
                    },
                    "state": {
                        "type": "string",
                        "description": "Optional: Filter by job state (COMPLETED, FAILED, CANCELLED, etc.)"
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Hours to look back (default: 24, max: 720)",
                        "default": 24
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results to return (default: 50, max: 500)",
                        "default": 50
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_help",
            "description": "Show help information about available commands and how to use Bob. Use when users ask for help.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Set a reminder that fires at a specific UTC time. Use when users ask to be reminded about something. Convert relative times ('in 2 minutes', 'tomorrow at 9am', 'every Monday') to ISO UTC using the current time from the system prompt. Always confirm the scheduled UTC time back to the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "What to remind the user about, e.g. 'Check backups'"
                    },
                    "fire_at": {
                        "type": "string",
                        "description": "ISO 8601 UTC datetime when the reminder should fire, e.g. '2026-03-17T09:00:00Z'. Must be in the future."
                    },
                    "recurrence_minutes": {
                        "type": "integer",
                        "description": "0 = one-time reminder (default). >0 = repeat every N minutes. Examples: 60=hourly, 1440=daily, 10080=weekly.",
                        "default": 0
                    }
                },
                "required": ["message", "fire_at"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_reminders",
            "description": "List all pending reminders in this room. Use when users ask 'what reminders are set?' or 'show my reminders'.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_reminder",
            "description": "Delete a reminder by ID. Use when users say 'delete reminder N' or 'cancel reminder N'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {
                        "type": "integer",
                        "description": "The reminder ID to delete"
                    }
                },
                "required": ["reminder_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "snooze_reminder",
            "description": "Postpone a reminder by N minutes. Use when users say 'snooze reminder N for X minutes'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {
                        "type": "integer",
                        "description": "The reminder ID to snooze"
                    },
                    "snooze_minutes": {
                        "type": "integer",
                        "description": "How many minutes to push the reminder forward (must be > 0)"
                    }
                },
                "required": ["reminder_id", "snooze_minutes"]
            }
        }
    }
]

# Map tool names to functions
TOOL_FUNCTIONS = {
    "query_audit_log": audit.query_audit_log,
    "get_active_alerts": get_active_alerts,
    "get_infrastructure_summary": get_infrastructure_summary,
    "manage_alert": manage_alert,
    "run_command": run_command,
    "get_slurm_nodes": get_slurm_nodes,
    "manage_slurm_node": manage_slurm_node,
    "get_slurm_jobs": get_slurm_jobs,
    "get_slurm_job_details": get_slurm_job_details,
    "get_slurm_job_history": get_slurm_job_history,
    "get_help": get_help,
    "set_reminder":   reminders.set_reminder,
    "list_reminders": reminders.list_reminders,
    "delete_reminder": reminders.delete_reminder,
    "snooze_reminder": reminders.snooze_reminder,
}


# =============================================================================
# Bot Class
# =============================================================================

class RocketChatBot:
    """Rocket.Chat bot with direct Ollama integration for infrastructure monitoring."""

    def __init__(self):
        self.rc: Optional[RocketChat] = None
        self.room_ids: dict = {}  # channel_name -> room_id
        self.dm_room_ids: set = set()  # DM room IDs
        self.last_dm_refresh: Optional[datetime] = None
        self.first_poll_done: set = set()  # Track rooms that have been polled once
        self.start_time = datetime.now(timezone.utc)  # Used to skip pre-startup DM messages
        self.rc_outage_start: Optional[datetime] = None  # Set when polling starts failing
        self.rc_consecutive_failures: int = 0

        # ThreadPoolExecutor for message processing
        # max_workers = 2x concurrency to allow queuing while Ollama calls are in flight
        self.executor = ThreadPoolExecutor(
            max_workers=MAX_OLLAMA_CONCURRENCY * 2,
            thread_name_prefix="msg_worker_"
        )

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
            logger.warning("No channels configured - will only respond to DMs")

        if not self.room_ids and not RC_DM_USERS:
            logger.error("No channels and no DM users configured — nothing to monitor")
            return False

        return True

    def setup_dms(self):
        """Initial DM room discovery."""
        self.refresh_dm_rooms(force=True)

    def refresh_dm_rooms(self, force: bool = False):
        """Refresh DM room list periodically so new DMs are discovered."""
        if not force and DM_REFRESH_INTERVAL > 0 and self.last_dm_refresh:
            elapsed = (datetime.now(timezone.utc) - self.last_dm_refresh).total_seconds()
            if elapsed < DM_REFRESH_INTERVAL:
                return

        try:
            result = self.rc.im_list()
            if result.ok:
                ims = result.json().get("ims", [])
                new_dm_room_ids: set = set()

                for im in ims:
                    room_id = im.get("_id")
                    username = im.get("username", "unknown")
                    if room_id:
                        new_dm_room_ids.add(room_id)
                        if room_id not in self.dm_room_ids:
                            logger.info(f"New DM discovered: {username} ({room_id})")

                removed = self.dm_room_ids - new_dm_room_ids
                if removed:
                    logger.info(f"Removed {len(removed)} DM room(s) no longer visible")

                self.dm_room_ids = new_dm_room_ids
                self.last_dm_refresh = datetime.now(timezone.utc)

                if force and self.dm_room_ids:
                    logger.info(f"Monitoring {len(self.dm_room_ids)} DM conversations")
        except Exception as e:
            logger.warning(f"Could not refresh DM rooms: {e}")
            self.last_dm_refresh = datetime.now(timezone.utc)

    def should_respond_and_claim(self, message: dict, is_dm: bool = False) -> bool:
        """Check if bot should respond AND atomically claim message. Thread-safe."""
        msg_id = message.get("_id", "")

        # Atomic check-and-set to prevent duplicate processing
        with processed_messages_lock:
            if msg_id in processed_messages:
                return False
            # Claim message immediately (before spawning thread)
            processed_messages[msg_id] = True  # OrderedDict tracks insertion order

            # Cleanup old processed messages (FIFO - oldest first)
            if len(processed_messages) > MAX_PROCESSED_MESSAGES:
                processed_messages.popitem(last=False)  # Remove oldest

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
            if RC_PREFIX.lower() not in text.lower():
                return False

        return True

    def get_message_text(self, message: dict, is_dm: bool = False) -> str:
        """Extract and clean message text."""
        text = message.get("msg", "")

        # Remove prefix wherever it appears (start, middle, end) so the LLM
        # only sees the actual request without the bot's name in it.
        if not is_dm and RC_PREFIX:
            text = re.sub(re.escape(RC_PREFIX), "", text, flags=re.IGNORECASE).strip()

        return text

    def get_conversation_history(self, room_id: str, user: str) -> list:
        """Get conversation history for a room + user. Thread-safe."""
        conv_key = f"{room_id}:{user}"

        with conversations_lock:
            if conv_key in conversations:
                conv = conversations[conv_key]

                # Check if conversation is still active
                if CONVERSATION_TIMEOUT > 0:
                    elapsed = datetime.now(timezone.utc) - conv["last_activity"]
                    if elapsed > timedelta(seconds=CONVERSATION_TIMEOUT):
                        logger.info(f"Conversation expired for {user} in room {room_id}")
                        del conversations[conv_key]
                        return []

                # Return a copy to avoid race conditions
                return list(conv.get("messages", []))

            return []

    def update_conversation(self, room_id: str, user: str, user_msg: str, assistant_msg: str):
        """Update conversation history. Thread-safe."""
        conv_key = f"{room_id}:{user}"

        with conversations_lock:
            if conv_key not in conversations:
                conversations[conv_key] = {
                    "messages": [],
                    "last_activity": datetime.now(timezone.utc)
                }

            # Add messages to history
            conversations[conv_key]["messages"].append({"role": "user", "content": user_msg})
            conversations[conv_key]["messages"].append({"role": "assistant", "content": assistant_msg})
            conversations[conv_key]["last_activity"] = datetime.now(timezone.utc)

            # Keep only last 20 messages (10 exchanges)
            if len(conversations[conv_key]["messages"]) > 20:
                conversations[conv_key]["messages"] = conversations[conv_key]["messages"][-20:]

            # Evict oldest conversation if limit exceeded (LRU)
            if len(conversations) > MAX_CONVERSATIONS:
                oldest_key = min(conversations.keys(), key=lambda k: conversations[k]["last_activity"])
                logger.info(f"Evicting oldest conversation: {oldest_key}")
                del conversations[oldest_key]

    def inject_bot_message(self, room_id: str, user: str, text: str) -> None:
        """Store a bot-initiated message in conversation history for context."""
        conv_key = f"{room_id}:{user}"
        with conversations_lock:
            if conv_key not in conversations:
                conversations[conv_key] = {"messages": [], "last_activity": datetime.now(timezone.utc)}
            conversations[conv_key]["messages"].append({"role": "assistant", "content": text})
            conversations[conv_key]["last_activity"] = datetime.now(timezone.utc)
            if len(conversations[conv_key]["messages"]) > 20:
                conversations[conv_key]["messages"] = conversations[conv_key]["messages"][-20:]

    def call_ollama(self, text: str, room_id: str, user: str) -> str:
        """Send message to Ollama and get response with tool support."""
        import json as json_lib

        # Get conversation history
        history = self.get_conversation_history(room_id, user)

        # Build messages array
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        messages = [{"role": "system", "content": f"Current date and time: {now_str}\n\n{SYSTEM_PROMPT}"}]
        messages.extend(history)
        messages.append({"role": "user", "content": text})

        max_iterations = MAX_OLLAMA_ITERATIONS  # Prevent infinite loops
        iteration = 0
        executed_tool_calls: set = set()  # Track (tool_name, args_hash) to prevent duplicate calls

        try:
            while iteration < max_iterations:
                iteration += 1
                logger.info(f"Ollama iteration {iteration}/{max_iterations}")

                # Debug: Log messages being sent to Ollama
                logger.debug(f"Messages count: {len(messages)}")
                if iteration > 1:
                    logger.debug(f"Last message role: {messages[-1].get('role')}")
                    logger.debug(f"Last message content length: {len(str(messages[-1].get('content', '')))} chars")

                # Call Ollama with native tool calling (acquire semaphore to limit concurrency)
                start_time = time.time()
                with ollama_semaphore:  # Limit to 3 concurrent Ollama calls
                    response = requests.post(
                        f"{OLLAMA_URL}/api/chat",
                        json={
                            "model": OLLAMA_MODEL,
                            "messages": messages,
                            "tools": OLLAMA_TOOLS,  # Enable native tool calling
                            "stream": False,
                            "keep_alive": 300,  # Keep model loaded for 5 minutes after last use
                            "options": {
                                "temperature": OLLAMA_TEMPERATURE,  # Low temp = more factual, less creative/hallucination
                                "num_ctx": OLLAMA_NUM_CTX,  # Context window size (tokens)
                                "num_predict": 2048  # Max response length (prevents runaway generation)
                            }
                        },
                        timeout=180
                    )

                if response.status_code != 200:
                    logger.error(f"Ollama returned {response.status_code}: {response.text}")
                    return f"Error: Ollama API returned {response.status_code}"

                data = response.json()
                elapsed = time.time() - start_time
                logger.info(f"Ollama responded in {elapsed:.2f}s")

                assistant_message = data.get("message", {})
                content = assistant_message.get("content", "").strip()
                tool_calls = assistant_message.get("tool_calls", [])

                # Check if Ollama wants to call tools (native tool calling)
                if tool_calls:
                    logger.info(f"Ollama requested {len(tool_calls)} tool call(s)")

                    # Add assistant message with tool_calls to conversation
                    messages.append(assistant_message)

                    # Execute each tool call
                    for tool_call in tool_calls:
                        function_name = tool_call.get("function", {}).get("name")
                        function_args = tool_call.get("function", {}).get("arguments", {})

                        # Some providers return arguments as JSON string; normalize to dict.
                        if isinstance(function_args, str):
                            try:
                                function_args = json_lib.loads(function_args)
                            except Exception:
                                logger.warning(f"Could not parse tool arguments for {function_name}: {function_args}")
                                function_args = {}
                        if not isinstance(function_args, dict):
                            function_args = {}

                        # Dedup: skip if exact same tool+args already executed this session
                        call_key = (function_name, json_lib.dumps(function_args, sort_keys=True))
                        if call_key in executed_tool_calls:
                            logger.warning(f"Skipping duplicate tool call: {function_name} with args {function_args}")
                            messages.append({
                                "role": "tool",
                                "content": f"⚠️ This tool was already called with these exact arguments in this session. Do not call it again. Use the earlier result and provide your final answer to the user now."
                            })
                            continue

                        logger.info(f"Executing tool: {function_name} with args: {function_args}")

                        # Get the tool function
                        tool_func = TOOL_FUNCTIONS.get(function_name)

                        if tool_func:
                            try:
                                # Call the tool with unpacked arguments
                                if function_name == "get_active_alerts":
                                    tool_result = tool_func(
                                        min_severity=function_args.get("min_severity", 0),
                                        limit=function_args.get("limit", 25),
                                        host=function_args.get("host", "")
                                    )
                                elif function_name == "get_infrastructure_summary":
                                    tool_result = tool_func()
                                elif function_name == "manage_alert":
                                    tool_result = tool_func(
                                        event_id=function_args.get("event_id"),
                                        action=function_args.get("action", "acknowledge"),
                                        message=function_args.get("message", ""),
                                        severity=function_args.get("severity"),
                                        suppress_hours=function_args.get("suppress_hours"),
                                        suppress_days=function_args.get("suppress_days")
                                    )
                                elif function_name == "run_command":
                                    tool_result = tool_func(
                                        host=function_args.get("host"),
                                        command=function_args.get("command")
                                    )
                                elif function_name == "get_slurm_nodes":
                                    tool_result = tool_func(
                                        partition=function_args.get("partition", "")
                                    )
                                elif function_name == "manage_slurm_node":
                                    tool_result = tool_func(
                                        action=function_args.get("action"),
                                        node=function_args.get("node"),
                                        reason=function_args.get("reason", ""),
                                        confirm=function_args.get("confirm", False)
                                    )
                                elif function_name == "get_slurm_jobs":
                                    tool_result = tool_func(
                                        user=function_args.get("user", ""),
                                        node=function_args.get("node", "")
                                    )
                                elif function_name == "get_slurm_job_details":
                                    tool_result = tool_func(
                                        jobid=function_args.get("jobid")
                                    )
                                elif function_name == "get_slurm_job_history":
                                    tool_result = tool_func(
                                        user=function_args.get("user", ""),
                                        state=function_args.get("state", ""),
                                        hours=function_args.get("hours", 24),
                                        limit=function_args.get("limit", 50)
                                    )
                                elif function_name == "query_audit_log":
                                    tool_result = tool_func(
                                        tool_name=function_args.get("tool_name", ""),
                                        node=function_args.get("node", ""),
                                        user=function_args.get("user", ""),
                                        hours=function_args.get("hours", 168),
                                        limit=function_args.get("limit", 20),
                                    )
                                elif function_name == "get_help":
                                    tool_result = tool_func()
                                elif function_name == "set_reminder":
                                    tool_result = tool_func(
                                        room_id=room_id,
                                        created_by=user,
                                        message=function_args.get("message", ""),
                                        fire_at=function_args.get("fire_at", ""),
                                        recurrence_minutes=int(function_args.get("recurrence_minutes", 0)),
                                    )
                                elif function_name == "list_reminders":
                                    tool_result = tool_func(room_id=room_id, created_by=user)
                                elif function_name == "delete_reminder":
                                    rid = function_args.get("reminder_id")
                                    if rid is None:
                                        tool_result = {"success": False, "error": "reminder_id not provided — check conversation history for a recently fired reminder ID, or call list_reminders and ask the user which one to delete"}
                                    else:
                                        tool_result = tool_func(reminder_id=int(rid))
                                elif function_name == "snooze_reminder":
                                    rid = function_args.get("reminder_id")
                                    if rid is None:
                                        tool_result = {"success": False, "error": "reminder_id not provided — check conversation history for a recently fired reminder ID, or call list_reminders and ask the user which one to snooze"}
                                    else:
                                        tool_result = tool_func(
                                            reminder_id=int(rid),
                                            snooze_minutes=int(function_args.get("snooze_minutes", 0)),
                                        )
                                else:
                                    tool_result = {"success": False, "error": f"Unknown tool: {function_name}"}

                                # Add tool result to messages
                                if tool_result.get("success"):
                                    result_data = tool_result.get("data", "No data returned")
                                    logger.debug(f"Tool result data length: {len(str(result_data))} chars")
                                    logger.debug(f"Tool result preview: {str(result_data)[:200]}...")
                                    # Prepend strong instruction to present the data
                                    tool_message = {
                                        "role": "tool",
                                        "content": f"Tool result:\n\n{result_data}"
                                    }
                                else:
                                    # Tool failed - make error very clear
                                    tool_message = {
                                        "role": "tool",
                                        "content": f"❌ ERROR: {tool_result.get('error', 'Unknown error')}\n\nIMPORTANT: Tell the user the tool failed. DO NOT invent data."
                                    }

                                messages.append(tool_message)
                                if tool_result.get('success'):
                                    logger.info(f"Tool {function_name} completed: success")
                                    executed_tool_calls.add(call_key)
                                else:
                                    logger.error(f"Tool {function_name} failed: {tool_result.get('error', 'Unknown error')}")

                                # Audit log — skip read-only tools; only record state-changing actions
                                if function_name not in ("query_audit_log", "get_help", "get_active_alerts",
                                                         "get_infrastructure_summary", "get_slurm_nodes",
                                                         "get_slurm_jobs", "get_slurm_job_details",
                                                         "get_slurm_job_history", "list_reminders"):
                                    audit.log_action(
                                        room_id=room_id,
                                        user=user,
                                        tool_name=function_name,
                                        args=function_args,
                                        success=bool(tool_result.get("success")),
                                        result_text=str(tool_result.get("data") or tool_result.get("error", "")),
                                        user_prompt=text,
                                    )

                            except Exception as e:
                                logger.error(f"Error executing tool {function_name}: {e}")
                                messages.append({
                                    "role": "tool",
                                    "content": f"❌ ERROR: Tool execution failed: {str(e)}"
                                })
                                if function_name not in ("query_audit_log", "get_help", "get_active_alerts",
                                                         "get_infrastructure_summary", "get_slurm_nodes",
                                                         "get_slurm_jobs", "get_slurm_job_details",
                                                         "get_slurm_job_history", "list_reminders"):
                                    audit.log_action(
                                        room_id=room_id,
                                        user=user,
                                        tool_name=function_name,
                                        args=function_args,
                                        success=False,
                                        result_text=f"Exception: {e}",
                                        user_prompt=text,
                                    )

                        else:
                            logger.error(f"Tool function not found: {function_name}")
                            messages.append({
                                "role": "tool",
                                "content": f"❌ ERROR: Tool '{function_name}' not found"
                            })

                    # Continue loop to let Ollama process tool results
                    continue

                # No native tool calls — check if model emitted tool calls as plain text JSON
                elif content:
                    parsed_text_tools = []
                    try:
                        stripped = content.strip()
                        if stripped.startswith("[") or stripped.startswith("{"):
                            parsed = json_lib.loads(stripped)
                            if isinstance(parsed, dict):
                                parsed = [parsed]
                            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and "name" in parsed[0]:
                                parsed_text_tools = parsed
                    except Exception:
                        pass

                    if parsed_text_tools:
                        logger.warning(f"Model emitted {len(parsed_text_tools)} tool call(s) as plain text — executing anyway")
                        # Inject as synthetic tool_calls so the existing loop handles them
                        synthetic = [{"function": {"name": t["name"], "arguments": t.get("arguments", {})}} for t in parsed_text_tools]
                        messages.append({"role": "assistant", "content": "", "tool_calls": synthetic})
                        tool_calls = synthetic
                        # Re-run tool execution block by jumping back to top of loop
                        # We fake the response structure so the next iteration picks it up
                        # Instead, execute inline by re-entering via continue after appending
                        # Simplest: just replace tool_calls and fall through to execution above
                        # — restructure: set tool_calls and redo the tool execution here
                        for tool_call in synthetic:
                            function_name = tool_call["function"]["name"]
                            function_args = tool_call["function"]["arguments"]
                            if isinstance(function_args, str):
                                try:
                                    function_args = json_lib.loads(function_args)
                                except Exception:
                                    function_args = {}
                            if not isinstance(function_args, dict):
                                function_args = {}

                            call_key = (function_name, json_lib.dumps(function_args, sort_keys=True))
                            if call_key in executed_tool_calls:
                                messages.append({"role": "tool", "content": "⚠️ Already called. Use earlier result."})
                                continue

                            logger.info(f"Executing text-fallback tool: {function_name} with args: {function_args}")
                            tool_func = TOOL_FUNCTIONS.get(function_name)
                            if not tool_func:
                                messages.append({"role": "tool", "content": f"❌ ERROR: Tool '{function_name}' not found"})
                                continue
                            try:
                                tool_result = tool_func(**{k: v for k, v in function_args.items()})
                                if tool_result.get("success"):
                                    executed_tool_calls.add(call_key)
                                    messages.append({"role": "tool", "content": f"Tool result:\n\n{tool_result.get('data', 'No data returned')}"})
                                else:
                                    messages.append({"role": "tool", "content": f"❌ ERROR: {tool_result.get('error', 'Unknown error')}"})
                                if function_name not in ("query_audit_log", "get_help", "get_active_alerts",
                                                         "get_infrastructure_summary", "get_slurm_nodes",
                                                         "get_slurm_jobs", "get_slurm_job_details",
                                                         "get_slurm_job_history", "list_reminders"):
                                    audit.log_action(room_id=room_id, user=user, tool_name=function_name,
                                                     args=function_args, success=bool(tool_result.get("success")),
                                                     result_text=str(tool_result.get("data") or tool_result.get("error", "")),
                                                     user_prompt=text)
                            except Exception as e:
                                logger.error(f"Text-fallback tool {function_name} raised: {e}")
                                messages.append({"role": "tool", "content": f"❌ ERROR: Tool execution failed: {e}"})
                        continue  # Let Ollama summarise the tool results

                    # Genuine final answer
                    self.update_conversation(room_id, user, text, content)
                    return content

                else:
                    # Empty content, no tool calls
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
        """Process a single message. Can run in parallel threads.

        Note: Message is already claimed by should_respond_and_claim() before this runs.
        """
        user = message.get("u", {}).get("username", "unknown")  # Needed in except too
        try:
            # Extract info
            text = self.get_message_text(message, is_dm=is_dm)

            if not text:
                return

            msg_type = "DM" if is_dm else "channel"
            logger.info(f"[{msg_type}] Message from {user}: {text[:100]}...")

            # Check for context reset command (check if ANY reset keyword is in the text)
            text_lower = text.lower().strip()
            reset_keywords = ["reset", "forget", "clear", "reset context", "forget conversation", "start over"]
            if any(keyword == text_lower or text_lower.endswith(f" {keyword}") or text_lower.startswith(f"{keyword} ") for keyword in reset_keywords):
                conv_key = f"{room_id}:{user}"
                # Check and delete conversation (release lock before network I/O)
                had_conversation = False
                with conversations_lock:
                    if conv_key in conversations:
                        del conversations[conv_key]
                        had_conversation = True
                        logger.info(f"Cleared conversation context for {user} in room {room_id}")

                # Send response outside lock (network I/O)
                if had_conversation:
                    self.send_message(room_id, "Conversation context cleared. Starting fresh! 🔄")
                else:
                    self.send_message(room_id, "No conversation context to clear. Already starting fresh! ✨")
                return

            # Show typing indicator while Ollama is thinking.
            # The indicator expires after ~10s so refresh it every 8s in a thread.
            typing_stop = threading.Event()

            def _keep_typing():
                try:
                    self.rc.rooms_typing(room_id=room_id, typing=True)
                except Exception:
                    pass
                while not typing_stop.wait(timeout=8):
                    try:
                        self.rc.rooms_typing(room_id=room_id, typing=True)
                    except Exception:
                        pass

            typing_thread = threading.Thread(target=_keep_typing, daemon=True)
            typing_thread.start()

            try:
                response = self.call_ollama(text, room_id, user)
            finally:
                typing_stop.set()
                try:
                    self.rc.rooms_typing(room_id=room_id, typing=False)
                except Exception:
                    pass

            # Send response
            self.send_message(room_id, response)
            logger.info(f"Sent response ({len(response)} chars)")

        except Exception as e:
            logger.exception(f"Fatal error processing message from {user}: {e}")
            try:
                self.send_message(room_id, "⚠️ An internal error occurred while processing your request. Please try again or contact the team if the issue persists.")
            except Exception:
                pass  # Best effort

    def _on_poll_error(self, context: str, error: Exception) -> None:
        """Track consecutive poll failures and log appropriately."""
        if self.rc_outage_start is None:
            self.rc_outage_start = datetime.now(timezone.utc)
            self.rc_consecutive_failures = 0
            logger.warning(f"Connection outage started ({context}): {error}")
        self.rc_consecutive_failures += 1
        # Periodic reminder every 30 failures to avoid log spam
        if self.rc_consecutive_failures % 30 == 0:
            elapsed = datetime.now(timezone.utc) - self.rc_outage_start
            logger.warning(f"Still unreachable after {elapsed} ({self.rc_consecutive_failures} consecutive failures)")

    def _on_poll_success(self) -> None:
        """Called after any successful poll. Posts recovery notice if coming back from outage."""
        if self.rc_outage_start is None:
            return

        duration = datetime.now(timezone.utc) - self.rc_outage_start
        total_seconds = int(duration.total_seconds())
        failures = self.rc_consecutive_failures

        # Reset state immediately so concurrent calls don't double-post
        self.rc_outage_start = None
        self.rc_consecutive_failures = 0

        # Only notify if outage was long enough to matter (>= 60s)
        if total_seconds < 60:
            logger.info(f"Brief connection hiccup resolved ({total_seconds}s, {failures} failures) — not notifying")
            return

        if total_seconds >= 3600:
            duration_str = f"{total_seconds // 3600}h {(total_seconds % 3600) // 60}m"
        elif total_seconds >= 60:
            duration_str = f"{total_seconds // 60}m {total_seconds % 60}s"
        else:
            duration_str = f"{total_seconds}s"

        msg = (f"⚠️ Connection restored after {duration_str} outage (SK\u2192US VPN link). "
               f"{failures} poll cycles missed.")
        logger.info(f"Connection restored after {duration_str} ({failures} failures) — notifying rooms")

        for room_id in self.room_ids.values():
            try:
                self.send_message(room_id, msg)
            except Exception:
                pass

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
                    self._on_poll_success()
                    messages = result.json().get("messages", [])

                    # On first poll, just mark all messages as seen without responding
                    if room_id not in self.first_poll_done:
                        logger.info(f"First poll of {channel_name}, marking {len(messages)} messages as seen")
                        with processed_messages_lock:
                            for message in messages:
                                msg_id = message.get("_id", "")
                                if msg_id:
                                    processed_messages[msg_id] = True
                        self.first_poll_done.add(room_id)
                        continue

                    # Process oldest first (using ThreadPoolExecutor)
                    for message in reversed(messages):
                        if self.should_respond_and_claim(message, is_dm=False):
                            # Submit to thread pool for parallel processing
                            self.executor.submit(self.process_message, message, room_id, False)

            except Exception as e:
                self._on_poll_error(channel_name, e)
                logger.error(f"Error polling {channel_name}: {e}")

    def poll_dms(self):
        """Poll for new messages in DMs."""
        for room_id in self.dm_room_ids:
            try:
                result = self.rc.im_history(room_id=room_id, count=10)

                if result.ok:
                    self._on_poll_success()
                    messages = result.json().get("messages", [])

                    # On first poll, mark pre-startup messages as seen, process post-startup ones
                    dm_key = f"dm_{room_id}"
                    if dm_key not in self.first_poll_done:
                        old_count = 0
                        recent_count = 0

                        for message in messages:
                            msg_id = message.get("_id", "")
                            if not msg_id:
                                continue

                            # Check message timestamp vs bot start time
                            msg_time_str = message.get("ts", "")
                            try:
                                # Rocket.Chat timestamps are ISO 8601 format
                                # Remove trailing Z and parse
                                if msg_time_str.endswith('Z'):
                                    msg_time_str = msg_time_str[:-1] + '+00:00'
                                msg_time = datetime.fromisoformat(msg_time_str)

                                # Messages from before bot started: skip (already handled or pre-existing)
                                if msg_time < self.start_time:
                                    with processed_messages_lock:
                                        processed_messages[msg_id] = True
                                    old_count += 1
                                else:
                                    # Message arrived after bot started: process normally
                                    recent_count += 1
                            except Exception as e:
                                # Can't parse timestamp: assume pre-startup, mark as seen
                                logger.warning(f"Failed to parse timestamp '{msg_time_str}': {e}")
                                with processed_messages_lock:
                                    processed_messages[msg_id] = True
                                old_count += 1

                        logger.info(f"First poll of DM {room_id}: marked {old_count} pre-startup messages as seen, will process {recent_count} post-startup")
                        self.first_poll_done.add(dm_key)
                        # Don't continue - fall through to process recent messages

                    # Process oldest first (using ThreadPoolExecutor)
                    for message in reversed(messages):
                        if self.should_respond_and_claim(message, is_dm=True):
                            # Submit to thread pool for parallel processing
                            self.executor.submit(self.process_message, message, room_id, True)

            except Exception as e:
                self._on_poll_error(f"DM {room_id}", e)
                logger.debug(f"Error polling DM {room_id}: {e}")

    def check_reminders(self):
        """Fire any reminders whose fire_at <= now."""
        due = reminders.get_due_reminders()
        for r in due:
            is_dm = r["room_id"] in self.dm_room_ids
            bot_prefix = "" if is_dm else RC_PREFIX
            text = reminders.format_fired_message(r, bot_prefix=bot_prefix)
            try:
                self.send_message(r["room_id"], text)
                reminders.mark_fired(r["id"], r["recurrence_minutes"])
                self.inject_bot_message(r["room_id"], r["created_by"], text)
            except Exception as e:
                logger.error(f"Failed to fire reminder id={r['id']}: {e}")
                # Leave in DB — retry next poll cycle

    def shutdown(self):
        """Shutdown bot gracefully, waiting for in-flight work."""
        logger.info("Shutting down bot...")
        logger.info("Waiting for in-flight message processing to complete...")
        self.executor.shutdown(wait=True)
        logger.info("Bot shutdown complete")

    def run(self):
        """Main bot loop."""
        logger.info("Starting Rocket.Chat <-> Ollama bot...")

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

        while not shutdown_event.is_set():
            try:
                self.poll_messages()
                self.refresh_dm_rooms()
                self.poll_dms()
                self.check_reminders()
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

        # Graceful shutdown
        self.shutdown()


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
    # RC_CHANNELS is optional - bot can run DM-only
    if not OLLAMA_URL:
        errors.append("OLLAMA_URL not configured")
    if not OLLAMA_MODEL:
        errors.append("OLLAMA_MODEL not configured")
    if not ZABBIX_PROXY_TOKEN or "CHANGE_THIS" in ZABBIX_PROXY_TOKEN:
        errors.append("ZABBIX_PROXY_TOKEN not configured")
    if not SSH_PROXY_TOKEN or "CHANGE_THIS" in SSH_PROXY_TOKEN:
        errors.append("SSH_PROXY_TOKEN not configured")

    if errors:
        logger.error("Configuration errors:")
        for err in errors:
            logger.error(f"  - {err}")
        logger.error("")
        logger.error("Set these in .env file (copy from .env.example)")
        return False

    return True


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    sig_name = signal.Signals(signum).name
    logger.info(f"Received {sig_name}, initiating graceful shutdown...")
    shutdown_event.set()


def main():
    """Entry point."""
    global ollama_semaphore

    # Setup signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if not validate_config():
        sys.exit(1)

    # Initialize semaphore with configured concurrency limit
    ollama_semaphore = threading.Semaphore(MAX_OLLAMA_CONCURRENCY)
    logger.info(f"Ollama concurrency limit: {MAX_OLLAMA_CONCURRENCY}")

    audit.init_db()
    reminders.init_db()

    bot = RocketChatBot()
    bot.run()


if __name__ == "__main__":
    main()

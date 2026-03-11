#!/usr/bin/env python3
"""
Audit log for rc-bot.

Records every tool action the bot takes (manage_alert, manage_slurm_node, etc.)
to a local SQLite database. Provides a query function registered as an Ollama
tool so the bot can answer "when did you drain gpu001 and why?".
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "audit.db"
RESULT_MAX_LEN = 2000   # Truncate long tool outputs stored in the DB

_write_lock = threading.Lock()


def init_db() -> None:
    """Create DB and tables if they don't exist. Call once at startup."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                room_id     TEXT    NOT NULL,
                user        TEXT    NOT NULL,
                tool_name   TEXT    NOT NULL,
                args_json   TEXT    NOT NULL,
                success     INTEGER NOT NULL,
                result_text TEXT    NOT NULL,
                user_prompt TEXT    DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_tool      ON audit_log(tool_name);
            CREATE INDEX IF NOT EXISTS idx_audit_user      ON audit_log(user);
            PRAGMA journal_mode=WAL;
        """)
        conn.commit()
        logger.info(f"Audit log initialised at {DB_PATH}")
    finally:
        conn.close()


def log_action(
    room_id: str,
    user: str,
    tool_name: str,
    args: dict,
    success: bool,
    result_text: str,
    user_prompt: str = "",
) -> None:
    """Insert one audit row. Thread-safe."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    truncated = result_text[:RESULT_MAX_LEN] if len(result_text) > RESULT_MAX_LEN else result_text

    with _write_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                "INSERT INTO audit_log "
                "(timestamp, room_id, user, tool_name, args_json, success, result_text, user_prompt) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    timestamp, room_id, user, tool_name,
                    json.dumps(args, ensure_ascii=False),
                    1 if success else 0,
                    truncated,
                    user_prompt[:500],  # Keep prompt short
                )
            )
            conn.commit()
        except Exception as e:
            logger.warning(f"Audit log_action failed: {e}")
        finally:
            conn.close()


def query_audit_log(
    tool_name: str = "",
    node: str = "",
    user: str = "",
    hours: int = 168,
    limit: int = 20,
) -> dict:
    """Query the audit log for past bot actions.

    Returns {"success": True, "data": str} or {"success": False, "error": str}.
    Registered as an Ollama tool so the bot can answer historical questions.
    """
    try:
        hours = max(1, min(int(hours), 8760))    # 1 hour – 1 year
        limit = max(1, min(int(limit), 200))

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

        conditions = ["timestamp >= ?"]
        params: list = [cutoff]

        if tool_name:
            conditions.append("tool_name = ?")
            params.append(tool_name)

        if user:
            conditions.append("user = ?")
            params.append(user)

        # node/host: search inside the JSON args string
        if node:
            conditions.append("args_json LIKE ?")
            params.append(f'%"{node}"%')

        where = " AND ".join(conditions)
        params.append(limit)

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"SELECT timestamp, user, tool_name, args_json, success, result_text, user_prompt "
                f"FROM audit_log WHERE {where} ORDER BY timestamp DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return {"success": True, "data": "No audit log entries found matching those criteria."}

        lines = [f"Found {len(rows)} audit log entries (most recent first):\n"]
        for row in rows:
            status = "✅ OK" if row["success"] else "❌ FAILED"
            try:
                args = json.loads(row["args_json"])
                args_str = ", ".join(f"{k}={v}" for k, v in args.items())
            except Exception:
                args_str = row["args_json"]

            lines.append(
                f"[{row['timestamp']}] {status} | {row['tool_name']}({args_str})"
                f" | requested by: {row['user']}"
                f"\n  Request: {row['user_prompt'][:200]}"
                f"\n  Result:  {row['result_text'][:300]}"
            )

        return {"success": True, "data": "\n\n".join(lines)}

    except Exception as e:
        logger.error(f"Audit query failed: {e}")
        return {"success": False, "error": f"Audit query failed: {e}"}

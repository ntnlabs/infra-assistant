#!/usr/bin/env python3
"""
Reminder service for rc-bot.

Stores and fires user-defined reminders backed by a local SQLite database.
Supports one-shot and recurring reminders with snooze and delete operations.
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("REMINDERS_DB_PATH", str(Path(__file__).parent / "reminders.db")))

_write_lock = threading.Lock()

_FORMATS = [
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%MZ",
]


def init_db() -> None:
    """Create table, indices, and enable WAL. Call once at startup."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS reminders (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id             TEXT    NOT NULL,
                created_by          TEXT    NOT NULL,
                message             TEXT    NOT NULL,
                fire_at             TEXT    NOT NULL,
                recurrence_minutes  INTEGER NOT NULL DEFAULT 0,
                created_at          TEXT    NOT NULL,
                last_fired_at       TEXT    DEFAULT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_fire_at ON reminders(fire_at);
            CREATE INDEX IF NOT EXISTS idx_reminders_room    ON reminders(room_id);
            CREATE INDEX IF NOT EXISTS idx_reminders_user    ON reminders(created_by);
            PRAGMA journal_mode=WAL;
        """)
        conn.commit()
        logger.info(f"Reminders DB initialised at {DB_PATH}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_fire_at(fire_at_str: str) -> datetime:
    """Parse fire_at string to UTC-aware datetime. Raises ValueError on failure."""
    for fmt in _FORMATS:
        try:
            dt = datetime.strptime(fire_at_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    raise ValueError(
        f"Cannot parse fire_at: {fire_at_str!r}. "
        "Use ISO UTC format, e.g. '2026-03-17T09:00:00Z'"
    )


def _fmt_recurrence(minutes: int) -> str:
    """Human-readable recurrence label, e.g. 'every day'."""
    if minutes >= 10080:
        weeks = minutes // 10080
        return f"every {'week' if weeks == 1 else f'{weeks} weeks'}"
    if minutes >= 1440:
        days = minutes // 1440
        return f"every {'day' if days == 1 else f'{days} days'}"
    if minutes >= 60:
        hours = minutes // 60
        return f"every {'hour' if hours == 1 else f'{hours} hours'}"
    return f"every {minutes} minutes"


def _fmt_next_in(minutes: int) -> str:
    """Human-readable 'next in ~X' label."""
    if minutes >= 10080:
        weeks = minutes // 10080
        return f"~{weeks} {'week' if weeks == 1 else 'weeks'}"
    if minutes >= 1440:
        days = minutes // 1440
        return f"~{days} {'day' if days == 1 else 'days'}"
    if minutes >= 60:
        hours = minutes // 60
        return f"~{hours} {'hour' if hours == 1 else 'hours'}"
    return f"~{minutes} minutes"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def set_reminder(
    room_id: str,
    created_by: str,
    message: str,
    fire_at: str,
    recurrence_minutes: int = 0,
) -> dict:
    """Create a new reminder.

    Returns {"success": True, "data": str} or {"success": False, "error": str}.
    """
    try:
        if not message or not message.strip():
            return {"success": False, "error": "message is required"}
        if not fire_at or not fire_at.strip():
            return {"success": False, "error": "fire_at is required"}

        fire_dt = _parse_fire_at(fire_at)
        now_utc = datetime.now(timezone.utc)

        if fire_dt <= now_utc:
            return {"success": False, "error": "fire_at must be in the future"}

        recurrence_minutes = max(0, int(recurrence_minutes))
        fire_at_iso = fire_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        created_at_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        with _write_lock:
            conn = sqlite3.connect(DB_PATH)
            try:
                cur = conn.execute(
                    "INSERT INTO reminders "
                    "(room_id, created_by, message, fire_at, recurrence_minutes, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (room_id, created_by, message.strip(), fire_at_iso,
                     recurrence_minutes, created_at_iso),
                )
                conn.commit()
                reminder_id = cur.lastrowid
            finally:
                conn.close()

        if recurrence_minutes > 0:
            desc = f"Reminder #{reminder_id} set for {fire_at_iso}, repeating {_fmt_recurrence(recurrence_minutes)}."
        else:
            desc = f"Reminder #{reminder_id} set for {fire_at_iso} (one-time)."

        return {"success": True, "data": desc}

    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"set_reminder failed: {e}")
        return {"success": False, "error": str(e)}


def list_reminders(room_id: str, created_by: str) -> dict:
    """List all pending reminders for a room, ordered by fire_at.

    Returns {"success": True, "data": str} or {"success": False, "error": str}.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, fire_at, message, recurrence_minutes, created_by "
                "FROM reminders WHERE room_id = ? ORDER BY fire_at ASC",
                (room_id,),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return {"success": True, "data": "No pending reminders."}

        lines = [f"Pending reminders ({len(rows)}):\n",
                 "ID  | Fire at (UTC)        | Recurrence   | Created by | Message"]
        lines.append("-" * 72)
        for r in rows:
            rec = _fmt_recurrence(r["recurrence_minutes"]) if r["recurrence_minutes"] else "one-time"
            lines.append(
                f"{r['id']:<4}| {r['fire_at']:<20} | {rec:<12} | {r['created_by']:<10} | {r['message']}"
            )

        return {"success": True, "data": "\n".join(lines)}

    except Exception as e:
        logger.error(f"list_reminders failed: {e}")
        return {"success": False, "error": str(e)}


def delete_reminder(reminder_id: int) -> dict:
    """Delete a reminder by ID.

    Returns {"success": True, "data": str} or {"success": False, "error": str}.
    """
    try:
        reminder_id = int(reminder_id)
        with _write_lock:
            conn = sqlite3.connect(DB_PATH)
            try:
                cur = conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
                conn.commit()
                deleted = cur.rowcount
            finally:
                conn.close()

        if deleted == 0:
            return {"success": False, "error": f"Reminder #{reminder_id} not found"}

        return {"success": True, "data": f"Reminder #{reminder_id} deleted."}

    except Exception as e:
        logger.error(f"delete_reminder failed: {e}")
        return {"success": False, "error": str(e)}


def snooze_reminder(reminder_id: int, snooze_minutes: int) -> dict:
    """Push a reminder's fire_at forward by snooze_minutes.

    Returns {"success": True, "data": str} or {"success": False, "error": str}.
    """
    try:
        reminder_id = int(reminder_id)
        snooze_minutes = int(snooze_minutes)

        if snooze_minutes <= 0:
            return {"success": False, "error": "snooze_minutes must be greater than 0"}

        with _write_lock:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT fire_at FROM reminders WHERE id = ?", (reminder_id,)
                ).fetchone()

                if not row:
                    return {"success": False, "error": f"Reminder #{reminder_id} not found"}

                old_dt = _parse_fire_at(row["fire_at"])
                new_dt = old_dt + timedelta(minutes=snooze_minutes)
                new_fire_at = new_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

                conn.execute(
                    "UPDATE reminders SET fire_at = ? WHERE id = ?",
                    (new_fire_at, reminder_id),
                )
                conn.commit()
            finally:
                conn.close()

        return {
            "success": True,
            "data": f"Reminder #{reminder_id} snoozed by {snooze_minutes} minutes. New fire time: {new_fire_at}",
        }

    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"snooze_reminder failed: {e}")
        return {"success": False, "error": str(e)}


def get_due_reminders() -> list:
    """Return all reminders whose fire_at <= now (UTC). Read-only, no lock needed."""
    try:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, room_id, created_by, message, fire_at, recurrence_minutes "
                "FROM reminders WHERE fire_at <= ? ORDER BY fire_at ASC",
                (now_iso,),
            ).fetchall()
        finally:
            conn.close()

        return [dict(r) for r in rows]

    except Exception as e:
        logger.error(f"get_due_reminders failed: {e}")
        return []


def mark_fired(reminder_id: int, recurrence_minutes: int) -> None:
    """After firing: delete one-shot reminders; reschedule recurring ones."""
    try:
        with _write_lock:
            conn = sqlite3.connect(DB_PATH)
            try:
                if recurrence_minutes == 0:
                    conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
                else:
                    now_utc = datetime.now(timezone.utc)
                    next_fire = (now_utc + timedelta(minutes=recurrence_minutes)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                    last_fired = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                    conn.execute(
                        "UPDATE reminders SET fire_at = ?, last_fired_at = ? WHERE id = ?",
                        (next_fire, last_fired, reminder_id),
                    )
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        logger.error(f"mark_fired failed for reminder id={reminder_id}: {e}")


def format_fired_message(r: dict, bot_prefix: str = "") -> str:
    """Build the chat message sent when a reminder fires."""
    user = r["created_by"]
    msg = r["message"]
    rid = r["id"]
    rec = r["recurrence_minutes"]

    header = f"@{user} ⏰ Reminder: **{msg}**"
    if rec == 0:
        return header  # reminder already deleted — no action hint

    recurrence_label = _fmt_recurrence(rec)
    next_in = _fmt_next_in(rec)
    mention = f"{bot_prefix} " if bot_prefix else ""
    footer = (
        f'_(Recurring: {recurrence_label} | next in {next_in} | ID: {rid} — '
        f'say "{mention}snooze reminder {rid}..." or "{mention}delete reminder {rid}")_'
    )
    return f"{header}\n{footer}"

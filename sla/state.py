# copyright by berlonak
# telegram: @Kilax123
"""Persist independent unanswered/unread SLA clocks and per-recipient delivery status.

The legacy tracked_chats_v2 table is not changed, so an existing v0.3.1
sla_state.sqlite3 can be used directly. Unread monitoring has its own table.
"""
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import logging
import time

LOGGER = logging.getLogger(__name__)


@dataclass
class ChatState:
    last_seen_id: int = 0
    first_unanswered_id: int | None = None
    first_unanswered_ts: int | None = None
    last_completed_ts: int | None = None
    reminders_sent: int = 0
    pending_main: bool = False
    pending_operator: bool = False

    def reset_alerts(self):
        self.last_completed_ts = None
        self.reminders_sent = 0
        self.pending_main = False
        self.pending_operator = False


@dataclass
class UnreadState:
    """The unread clock begins at the oldest currently unread incoming message."""
    last_seen_id: int = 0
    ignored_through_id: int = 0  # ignore old unread when first-start alerts are disabled
    first_unread_id: int | None = None
    first_unread_ts: int | None = None
    last_read_max_id: int = 0
    last_unread_count: int = 0
    last_completed_ts: int | None = None
    reminders_sent: int = 0
    pending_main: bool = False
    pending_operator: bool = False
    read_marker_known: bool = False  # migrated legacy rows wait for a fresh server marker

    def reset_alerts(self):
        self.last_completed_ts = None
        self.reminders_sent = 0
        self.pending_main = False
        self.pending_operator = False


class State:
    def __init__(self, filename):
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS tracked_chats_v2 (
             session TEXT NOT NULL,
             chat_id INTEGER NOT NULL,
             last_seen_id INTEGER NOT NULL DEFAULT 0,
             first_unanswered_id INTEGER,
             first_unanswered_ts INTEGER,
             last_completed_ts INTEGER,
             reminders_sent INTEGER NOT NULL DEFAULT 0,
             pending_main INTEGER NOT NULL DEFAULT 0,
             pending_operator INTEGER NOT NULL DEFAULT 0,
             PRIMARY KEY (session, chat_id)
        )""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS tracked_unread_v1 (
             session TEXT NOT NULL,
             chat_id INTEGER NOT NULL,
             last_seen_id INTEGER NOT NULL DEFAULT 0,
             ignored_through_id INTEGER NOT NULL DEFAULT 0,
             first_unread_id INTEGER,
             first_unread_ts INTEGER,
             last_read_max_id INTEGER NOT NULL DEFAULT 0,
             last_unread_count INTEGER NOT NULL DEFAULT 0,
             last_completed_ts INTEGER,
             reminders_sent INTEGER NOT NULL DEFAULT 0,
             pending_main INTEGER NOT NULL DEFAULT 0,
             pending_operator INTEGER NOT NULL DEFAULT 0,
             PRIMARY KEY (session, chat_id)
        )""")
        # Existing v0.5.1 databases have this table without read_marker_known.
        # Unknown is the safe migration value: no new unread alert is trusted
        # until Telegram supplies a fresh read_inbox_max_id.
        unread_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(tracked_unread_v1)")}
        if "read_marker_known" not in unread_columns:
            self.conn.execute("ALTER TABLE tracked_unread_v1 "
                              "ADD COLUMN read_marker_known INTEGER NOT NULL DEFAULT 0")
        self.conn.commit()
        from .statistics import install
        install(self.conn)
        # One row per actual bot message: main/operator + initial/repeats.
        # Older databases upgrade automatically; previous SLA clocks are untouched.
        self.conn.execute("""CREATE TABLE IF NOT EXISTS bot_alert_messages_v1 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('unread', 'unanswered')),
            case_message_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            bot_message_id INTEGER NOT NULL,
            original_html TEXT NOT NULL,
            resolved_ts INTEGER,
            edit_status TEXT NOT NULL DEFAULT 'open',
            retry_after_ts INTEGER NOT NULL DEFAULT 0,
            edit_attempts INTEGER NOT NULL DEFAULT 0,
            UNIQUE(recipient_id, bot_message_id)
        )""")
        self.conn.execute("""CREATE INDEX IF NOT EXISTS bot_alert_messages_pending_v1
            ON bot_alert_messages_v1(edit_status, retry_after_ts)""")
        self.conn.execute("""CREATE INDEX IF NOT EXISTS bot_alert_messages_case_v1
            ON bot_alert_messages_v1(session, chat_id, kind, case_message_id)""")
        # v0.5.5: why an alert was closed ('deleted' = the lead deleted the message).
        alert_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(bot_alert_messages_v1)")}
        if "resolution" not in alert_columns:
            self.conn.execute("ALTER TABLE bot_alert_messages_v1 ADD COLUMN resolution TEXT")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS meta_v1 (
            key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        self.conn.commit()

    def get(self, session, chat_id):
        row = self.conn.execute(
            """SELECT last_seen_id, first_unanswered_id, first_unanswered_ts,
                      last_completed_ts, reminders_sent, pending_main, pending_operator
               FROM tracked_chats_v2 WHERE session=? AND chat_id=?""",
            (session, chat_id),
        ).fetchone()
        if row is None:
            return None
        return ChatState(*row[:5], bool(row[5]), bool(row[6]))

    def save(self, session, chat_id, s):
        self.conn.execute("""INSERT INTO tracked_chats_v2
            (session, chat_id, last_seen_id, first_unanswered_id, first_unanswered_ts,
             last_completed_ts, reminders_sent, pending_main, pending_operator)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session, chat_id) DO UPDATE SET
              last_seen_id=excluded.last_seen_id,
              first_unanswered_id=excluded.first_unanswered_id,
              first_unanswered_ts=excluded.first_unanswered_ts,
              last_completed_ts=excluded.last_completed_ts,
              reminders_sent=excluded.reminders_sent,
              pending_main=excluded.pending_main,
              pending_operator=excluded.pending_operator""",
            (session, chat_id, s.last_seen_id, s.first_unanswered_id, s.first_unanswered_ts,
             s.last_completed_ts, s.reminders_sent, int(s.pending_main), int(s.pending_operator)),
        )
        self.conn.commit()

    def get_unread(self, session, chat_id):
        row = self.conn.execute("""SELECT last_seen_id, ignored_through_id, first_unread_id,
                       first_unread_ts, last_read_max_id, last_unread_count, last_completed_ts,
                       reminders_sent, pending_main, pending_operator, read_marker_known
                 FROM tracked_unread_v1 WHERE session=? AND chat_id=?""",
                                (session, chat_id)).fetchone()
        if row is None:
            return None
        return UnreadState(*row[:8], bool(row[8]), bool(row[9]), bool(row[10]))

    def save_unread(self, session, chat_id, s):
        self.conn.execute("""INSERT INTO tracked_unread_v1 (
            session, chat_id, last_seen_id, ignored_through_id, first_unread_id,
            first_unread_ts, last_read_max_id, last_unread_count, last_completed_ts,
            reminders_sent, pending_main, pending_operator, read_marker_known)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session, chat_id) DO UPDATE SET
              last_seen_id=excluded.last_seen_id,
              ignored_through_id=excluded.ignored_through_id,
              first_unread_id=excluded.first_unread_id,
              first_unread_ts=excluded.first_unread_ts,
              last_read_max_id=excluded.last_read_max_id,
              last_unread_count=excluded.last_unread_count,
              last_completed_ts=excluded.last_completed_ts,
              reminders_sent=excluded.reminders_sent,
              pending_main=excluded.pending_main,
              pending_operator=excluded.pending_operator,
              read_marker_known=excluded.read_marker_known""",
            (session, chat_id, s.last_seen_id, s.ignored_through_id, s.first_unread_id,
             s.first_unread_ts, s.last_read_max_id, s.last_unread_count,
             s.last_completed_ts, s.reminders_sent, int(s.pending_main), int(s.pending_operator),
             int(s.read_marker_known)))
        self.conn.commit()

    def record_alert(self, session, chat_id, kind, case_message_id,
                     recipient_id, bot_message_id, original_html):
        if case_message_id is None or bot_message_id is None:
            return
        LOGGER.debug('Сохранить уведомление: session=%s чат=%s kind=%s case=%s '
                     'кому=%s bot_message_id=%s', session, chat_id, kind,
                     case_message_id, recipient_id, bot_message_id)
        self.conn.execute("""INSERT OR IGNORE INTO bot_alert_messages_v1
            (session, chat_id, kind, case_message_id, recipient_id, bot_message_id, original_html)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session, chat_id, kind, case_message_id,
             int(recipient_id), int(bot_message_id), original_html))
        self.conn.commit()

    def mark_resolved(self, session, chat_id, kind, case_message_id, resolved_ts, reason=None):
        """reason=None: read/answered; 'deleted': the lead deleted the message."""
        if case_message_id is None:
            return
        cursor = self.conn.execute("""UPDATE bot_alert_messages_v1
            SET resolved_ts=?, edit_status='pending', retry_after_ts=0, resolution=?
            WHERE session=? AND chat_id=? AND kind=? AND case_message_id=?
              AND edit_status='open'""",
            (int(resolved_ts), reason, session, chat_id, kind, int(case_message_id)))
        self.conn.commit()
        LOGGER.info('Уведомления решены: session=%s чат=%s тип=%s case=%s '
                    'в очередь редактирования=%s', session, chat_id, kind,
                    case_message_id, cursor.rowcount)

    def pending_edits(self, now_ts, limit=8):
        rows = self.conn.execute("""SELECT id, recipient_id, bot_message_id,
            original_html, kind, resolved_ts, edit_attempts, resolution
            FROM bot_alert_messages_v1
            WHERE edit_status='pending' AND retry_after_ts<=?
            ORDER BY resolved_ts, id LIMIT ?""", (now_ts, limit)).fetchall()
        LOGGER.debug('Очередь редактирования: доступно=%d лимит=%d', len(rows), limit)
        return rows

    def retry_edit(self, row_id, retry_after_ts):
        LOGGER.debug('Повторить изменение уведомления id=%s после=%s', row_id, retry_after_ts)
        self.conn.execute("""UPDATE bot_alert_messages_v1
            SET edit_attempts=edit_attempts+1, retry_after_ts=? WHERE id=?""",
            (int(retry_after_ts), row_id))
        self.conn.commit()

    def complete_edit(self, row_id):
        LOGGER.debug('Изменение уведомления завершено: очередь id=%s', row_id)
        # A successfully edited message no longer needs a record.
        self.conn.execute("DELETE FROM bot_alert_messages_v1 WHERE id=?", (row_id,))
        self.conn.commit()

    # --- v0.5.5 maintenance: stale cases never block statistics forever ---

    def get_meta(self, key):
        row = self.conn.execute("SELECT value FROM meta_v1 WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.conn.execute("INSERT INTO meta_v1 (key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        self.conn.commit()

    def open_case_chats(self, session):
        """Chats of one operator with an open unanswered/unread case or open statistics."""
        rows = self.conn.execute("""
            SELECT chat_id FROM tracked_chats_v2 WHERE session=? AND first_unanswered_id IS NOT NULL
            UNION SELECT chat_id FROM tracked_unread_v1 WHERE session=? AND first_unread_id IS NOT NULL
            UNION SELECT chat_id FROM stats_answers WHERE session=? AND closed_ts IS NULL
            UNION SELECT chat_id FROM stats_reads WHERE session=? AND closed_ts IS NULL""",
            (session, session, session, session)).fetchall()
        return {row[0] for row in rows}

    def forget_chat(self, session, chat_id, now_ts=None, sla_seconds=None):
        """Stop tracking a chat that is no longer visible (deleted chat or account,
        archived with include_archived: false, excluded by config); if it reappears
        it is treated as new. Its open statistics cases are removed, except ones
        already overdue: those are closed now so the breach stays counted."""
        self.conn.execute("DELETE FROM tracked_chats_v2 WHERE session=? AND chat_id=?", (session, chat_id))
        self.conn.execute("DELETE FROM tracked_unread_v1 WHERE session=? AND chat_id=?", (session, chat_id))
        for table in ("stats_answers", "stats_reads"):
            if now_ts is not None and sla_seconds is not None:
                self.conn.execute(f"""UPDATE {table} SET closed_ts=max(opened_ts, ?), withdrawn=1
                    WHERE session=? AND chat_id=? AND closed_ts IS NULL AND opened_ts + ? <= ?""",
                    (now_ts, session, chat_id, sla_seconds, now_ts))
            self.conn.execute(f"DELETE FROM {table} WHERE session=? AND chat_id=? AND closed_ts IS NULL",
                              (session, chat_id))
        self.conn.commit()

    def drop_orphan_stats(self, session):
        """An open statistics case must be the chat's CURRENT case; drop leftovers."""
        removed = 0
        for table, tracked, column in (("stats_answers", "tracked_chats_v2", "first_unanswered_id"),
                                       ("stats_reads", "tracked_unread_v1", "first_unread_id")):
            cursor = self.conn.execute(f"""DELETE FROM {table}
                WHERE session=? AND closed_ts IS NULL AND NOT EXISTS (
                    SELECT 1 FROM {tracked} t WHERE t.session={table}.session
                      AND t.chat_id={table}.chat_id AND t.{column}={table}.message_id)""", (session,))
            removed += cursor.rowcount
        self.conn.commit()
        return removed

    def expire_ancient_cases(self, cutoff_ts, key="v0.5.5:expire_ancient_cases"):
        """One-time upgrade cleanup: cases opened from years-old history.

        Such chats are forgotten, so the next pass re-examines them with the
        history window: a lead active within the window gets a proper case again,
        a dead conversation stays quiet. Returns the number of chats, or None if
        the cleanup already ran earlier.
        """
        if self.get_meta(key) is not None:
            return None
        chats = set()
        if cutoff_ts is not None:
            answered = self.conn.execute("""SELECT session, chat_id FROM tracked_chats_v2
                WHERE first_unanswered_ts IS NOT NULL AND first_unanswered_ts < ?""", (cutoff_ts,)).fetchall()
            unread = self.conn.execute("""SELECT session, chat_id FROM tracked_unread_v1
                WHERE first_unread_ts IS NOT NULL AND first_unread_ts < ?""", (cutoff_ts,)).fetchall()
            for session, chat_id in answered:
                self.conn.execute("DELETE FROM tracked_chats_v2 WHERE session=? AND chat_id=?", (session, chat_id))
            for session, chat_id in unread:
                self.conn.execute("DELETE FROM tracked_unread_v1 WHERE session=? AND chat_id=?", (session, chat_id))
            for table in ("stats_answers", "stats_reads"):
                self.conn.execute(f"DELETE FROM {table} WHERE closed_ts IS NULL AND opened_ts < ?", (cutoff_ts,))
            chats = set(answered) | set(unread)
        self.conn.execute("INSERT OR REPLACE INTO meta_v1 (key, value) VALUES (?, ?)",
                          (key, str(int(time.time()))))
        self.conn.commit()
        if chats:
            LOGGER.info("Обновление v0.5.5: закрыто устаревших обращений (старше окна истории): %s", len(chats))
        return len(chats)

    def close(self):
        self.conn.close()

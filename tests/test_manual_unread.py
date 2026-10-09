# copyright by berlonak
# telegram: @Kilax123
"""A manually re-marked chat must never reopen a message that was read."""
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from test_monitor import BASE, FakeClient, FakeMessage, OP, fake_cfg, fake_dialog
from test_resolutions import TrackedBot
from sla.monitor import process_dialog
from sla.resolutions import apply_pending_edits
from sla.state import State


def marked(messages, count, read_max):
    snapshot = fake_dialog(messages, unread_count=count, read_max_id=read_max)
    snapshot.dialog.unread_mark = True
    return snapshot


class ManualUnreadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = Path(self.tmp.name) / 'state.sqlite3'
        self.state = State(self.dbpath)
        self.bot = TrackedBot()
        self.cfg = fake_cfg(alert_mode='unread', stats_enabled=True,
                            edit_resolved_alerts=True, remind_every_seconds=120)

    async def asyncTearDown(self):
        self.state.close()
        self.tmp.cleanup()

    async def check(self, messages, unread_count, read_max_id, now=BASE, manual=False):
        dialog = (marked(messages, unread_count, read_max_id) if manual else
                  fake_dialog(messages, unread_count=unread_count, read_max_id=read_max_id))
        await process_dialog(self.cfg, OP, FakeClient(messages), dialog,
                             self.state, self.bot, 222, now)

    async def test_already_read_message_manual_mark_creates_no_alert(self):
        messages = [FakeMessage(10, 95)]
        # Fake a stale/inconsistent count of 1, to test our defense-in-depth.
        await self.check(messages, 1, 10, manual=True)
        self.assertIsNone(self.state.get_unread('ivan', 333).first_unread_id)
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.state.conn.execute('SELECT count(*) FROM stats_reads').fetchone()[0], 0)
        # Repeated unchanged snapshots do not cause extra scans or alerts.
        await self.check(messages, 1, 10, now=BASE + timedelta(hours=2), manual=True)
        self.assertEqual(self.bot.sent, [])

    async def test_instant_read_then_manual_remark_between_polls(self):
        messages = [FakeMessage(10, 95)]
        # First poll genuinely observes an old unread message.
        await self.check(messages, 1, 0)
        self.assertEqual(len(self.bot.sent), 2)
        self.assertEqual(self.state.get_unread('ivan', 333).first_unread_id, 10)
        await self.check(messages, 1, 0, now=BASE + timedelta(minutes=10))
        self.assertEqual(len(self.bot.sent), 4)  # main + operator, first + repeat
        # Operator reads immediately and manually marks unread BEFORE the next
        # poll: read_inbox_max_id has advanced even if unread_mark=True and
        # unread_count happens to be stale/nonzero.
        self.state.close()
        self.state = State(self.dbpath)
        await self.check(messages, 1, 10, now=BASE + timedelta(minutes=11), manual=True)
        current = self.state.get_unread('ivan', 333)
        self.assertIsNone(current.first_unread_id)
        self.assertEqual(current.last_read_max_id, 10)
        self.assertEqual(current.reminders_sent, 0)
        self.assertEqual(len(self.bot.sent), 4)
        now_ts = int((BASE + timedelta(minutes=11)).timestamp())
        self.assertEqual(len(self.state.pending_edits(now_ts)), 4)
        await apply_pending_edits(self.state, self.bot, self.cfg, now_ts=now_ts, pace_seconds=0)
        self.assertEqual(len(self.bot.edits), 4)
        for _, _, body in self.bot.edits:
            self.assertTrue(body.startswith('✅ <b>Лид прочитан</b>'))
        row = self.state.conn.execute('SELECT opened_ts, closed_ts FROM stats_reads').fetchone()
        self.assertEqual(row[1], now_ts)
        # A later manual unread flag cannot revive the cleared case.
        await self.check(messages, 0, 10, now=BASE + timedelta(hours=3), manual=True)
        self.assertEqual(len(self.bot.sent), 4)
        self.assertEqual(self.state.get_unread('ivan', 333).last_read_max_id, 10)

    async def test_real_new_incoming_after_manual_mark_has_own_clock(self):
        messages = [FakeMessage(10, 95), FakeMessage(11, 65)]
        await self.check(messages, 1, 10, manual=True)
        self.assertEqual(self.state.get_unread('ivan', 333).first_unread_id, 11)
        self.assertEqual(len(self.bot.sent), 2)
        self.assertEqual(self.state.get_unread('ivan', 333).first_unread_ts,
                         int(messages[1].date.timestamp()))

    async def test_missing_marker_pauses_existing_case_and_does_not_resolve(self):
        messages = [FakeMessage(10, 95)]
        await self.check(messages, 1, 0)
        self.assertEqual(len(self.bot.sent), 2)
        snapshot = fake_dialog(messages, unread_count=1)  # raw marker unavailable
        await process_dialog(self.cfg, OP, FakeClient(messages), snapshot,
                             self.state, self.bot, 222, BASE + timedelta(minutes=5))
        self.assertEqual(len(self.bot.sent), 2)
        self.assertEqual(self.state.get_unread('ivan', 333).first_unread_id, 10)
        self.assertEqual(self.state.pending_edits(int(BASE.timestamp()) + 300), [])
        # When read is verifiable again, close the old case normally.
        await self.check(messages, 0, 10, now=BASE + timedelta(minutes=6), manual=True)
        self.assertIsNone(self.state.get_unread('ivan', 333).first_unread_id)
        self.assertEqual(len(self.state.pending_edits(int(BASE.timestamp()) + 360)), 2)

    async def test_regressed_marker_does_not_erase_known_read_position(self):
        messages = [FakeMessage(10, 95), FakeMessage(11, 65)]
        await self.check(messages, 1, 10)
        self.assertEqual(self.state.get_unread('ivan', 333).first_unread_id, 11)
        self.assertEqual(len(self.bot.sent), 2)
        # An inconsistent snapshot cannot push us back to ID zero.
        await self.check(messages, 2, 0, now=BASE + timedelta(minutes=5), manual=True)
        self.assertEqual(self.state.get_unread('ivan', 333).last_read_max_id, 10)
        self.assertEqual(self.state.get_unread('ivan', 333).first_unread_id, 11)
        self.assertEqual(len(self.bot.sent), 2)
        await self.check(messages, 0, 11, now=BASE + timedelta(minutes=6))
        self.assertEqual(len(self.state.pending_edits(int(BASE.timestamp()) + 360)), 2)

    async def test_zero_count_without_matching_read_marker_not_false_green(self):
        messages = [FakeMessage(10, 95)]
        await self.check(messages, 1, 0)
        await self.check(messages, 0, 0, now=BASE + timedelta(minutes=5))
        self.assertEqual(self.state.get_unread('ivan', 333).first_unread_id, 10)
        self.assertEqual(self.state.pending_edits(int(BASE.timestamp()) + 300), [])
        self.assertEqual(len(self.bot.sent), 2)
        await self.check(messages, 0, 10, now=BASE + timedelta(minutes=6))
        self.assertEqual(len(self.state.pending_edits(int(BASE.timestamp()) + 360)), 2)

    async def test_existing_v051_db_adds_marker_flag_preserves_original_state(self):
        self.state.close()
        old = self.dbpath
        old.unlink()
        conn = sqlite3.connect(old)
        conn.execute('''CREATE TABLE tracked_unread_v1 (
            session TEXT NOT NULL, chat_id INTEGER NOT NULL, last_seen_id INTEGER NOT NULL DEFAULT 0,
            ignored_through_id INTEGER NOT NULL DEFAULT 0, first_unread_id INTEGER,
            first_unread_ts INTEGER, last_read_max_id INTEGER NOT NULL DEFAULT 0,
            last_unread_count INTEGER NOT NULL DEFAULT 0, last_completed_ts INTEGER,
            reminders_sent INTEGER NOT NULL DEFAULT 0, pending_main INTEGER NOT NULL DEFAULT 0,
            pending_operator INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (session, chat_id))''')
        conn.execute('''INSERT INTO tracked_unread_v1 VALUES
            ('ivan',333,10,0,10,1234,0,1,4567,3,0,0)''')
        conn.commit()
        conn.close()
        self.state = State(old)
        row = self.state.get_unread('ivan', 333)
        self.assertEqual((row.first_unread_id, row.last_completed_ts, row.reminders_sent),
                         (10, 4567, 3))
        self.assertFalse(row.read_marker_known)
        # Save/reopen without losing migration fields or previous alerts.
        self.state.save_unread('ivan', 333, row)
        self.state.close()
        self.state = State(old)
        self.assertEqual(self.state.get_unread('ivan', 333).reminders_sent, 3)
        messages = [FakeMessage(10, 95)]
        await self.check(messages, 0, 10, now=BASE + timedelta(minutes=11), manual=True)
        self.assertTrue(self.state.get_unread('ivan', 333).read_marker_known)
        self.assertIsNone(self.state.get_unread('ivan', 333).first_unread_id)

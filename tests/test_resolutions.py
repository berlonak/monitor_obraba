# copyright by berlonak
# telegram: @Kilax123
"""Bot-message edits: no Telegram network connection needed."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from test_monitor import BASE, FakeClient, FakeMessage, OP, fake_cfg, fake_dialog
from sla.monitor import process_dialog
from sla.resolutions import apply_pending_edits, resolved_text
from sla.notifier import Notifier
from sla.state import State


class TrackedBot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.next_id = 100
        self.edit_response = ("edited", None)

    async def send_tracked(self, recipient, body):
        self.next_id += 1
        self.sent.append((recipient, self.next_id, body))
        return True, self.next_id

    async def edit(self, recipient, message_id, body):
        self.edits.append((recipient, message_id, body))
        return self.edit_response

    async def send(self, recipient, body):
        ok, _ = await self.send_tracked(recipient, body)
        return ok


class ResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbfile = Path(self.tmp.name) / 'state.sqlite3'
        self.state = State(self.dbfile)
        self.bot = TrackedBot()
        self.cfg = fake_cfg(alert_mode='unread', stats_enabled=False,
                            edit_resolved_alerts=True, remind_every_seconds=600)

    async def asyncTearDown(self):
        self.state.close()
        self.tmp.cleanup()

    async def test_read_edits_initial_and_reminders_in_both_chats_after_restart(self):
        messages = [FakeMessage(1, 90)]
        client = FakeClient(messages)
        dialog = fake_dialog(messages, unread_count=1, read_max_id=0)
        await process_dialog(self.cfg, OP, client, dialog,
                             self.state, self.bot, 222, BASE)
        await process_dialog(self.cfg, OP, client, dialog,
                             self.state, self.bot, 222, BASE + timedelta(minutes=10))
        self.assertEqual(len(self.bot.sent), 4)
        self.assertEqual(set(who for who, _, _ in self.bot.sent), {111, 222})
        self.assertEqual(len(client.calls), 1)
        when = BASE + timedelta(minutes=11)
        await process_dialog(self.cfg, OP, client,
                             fake_dialog(messages, unread_count=0, read_max_id=1),
                             self.state, self.bot, 222, when)
        # Tracking an edit does not generate more messages, history reads, or
        # lose its work if the operator process is restarted before editing.
        self.assertEqual(len(self.bot.sent), 4)
        self.assertEqual(len(client.calls), 1)
        self.state.close()
        self.state = State(self.dbfile)
        self.assertEqual(len(self.state.pending_edits(int(when.timestamp()))), 4)
        count = await apply_pending_edits(
            self.state, self.bot, self.cfg, now_ts=int(when.timestamp()), pace_seconds=0)
        self.assertEqual(count, 4)
        self.assertEqual(len(self.bot.edits), 4)
        self.assertEqual(self.state.pending_edits(int(when.timestamp())), [])
        for (_, recipient, original), (_, mid, updated) in zip(self.bot.sent, self.bot.edits):
            self.assertEqual(mid, recipient)
            self.assertTrue(updated.startswith('✅ <b>Лид прочитан</b>'))
            self.assertIn('10:11:00 (UTC+3)', updated)
            self.assertIn('На момент уведомления:', updated)
            self.assertIn('открыть чат', updated)
            self.assertIn('Лид:', original)

    async def test_answer_edits_at_outgoing_message_time(self):
        cfg = dict(self.cfg, alert_mode='unanswered')
        messages = [FakeMessage(1, 90)]
        client = FakeClient(messages)
        await process_dialog(cfg, OP, client, fake_dialog(messages, unread_count=1),
                             self.state, self.bot, 222, BASE)
        self.assertEqual(len(self.bot.sent), 2)
        reply_time = BASE + timedelta(minutes=3)
        client.messages.append(FakeMessage(2, 0, out=True, base=reply_time))
        await process_dialog(cfg, OP, client, fake_dialog(client.messages, unread_count=1),
                             self.state, self.bot, 222, BASE + timedelta(minutes=15))
        count = await apply_pending_edits(self.state, self.bot, cfg,
                                          now_ts=int((BASE + timedelta(minutes=15)).timestamp()),
                                          pace_seconds=0)
        self.assertEqual(count, 2)
        for _, _, body in self.bot.edits:
            self.assertTrue(body.startswith('✅ <b>Лиду ответили</b>'))
            self.assertIn('10:03:00 (UTC+3)', body)
        self.assertEqual(self.state.get('ivan', 333).first_unanswered_id, None)

    async def test_both_clears_modes_independently(self):
        cfg = dict(self.cfg, alert_mode='both')
        messages = [FakeMessage(1, 90)]
        client = FakeClient(messages)
        await process_dialog(cfg, OP, client, fake_dialog(messages, unread_count=1, read_max_id=0),
                             self.state, self.bot, 222, BASE)
        self.assertEqual(len(self.bot.sent), 4)
        await process_dialog(cfg, OP, client, fake_dialog(messages, unread_count=0, read_max_id=1),
                             self.state, self.bot, 222, BASE + timedelta(minutes=1))
        rows = self.state.pending_edits(int((BASE + timedelta(minutes=1)).timestamp()))
        self.assertEqual(len(rows), 2)
        self.assertEqual({r[4] for r in rows}, {'unread'})
        # An outstanding answer is still overdue despite being read.
        self.assertEqual(self.state.get('ivan', 333).first_unanswered_id, 1)
        client.messages.append(FakeMessage(2, 0, out=True, base=BASE + timedelta(minutes=2)))
        await process_dialog(cfg, OP, client,
                             fake_dialog(client.messages, unread_count=0, read_max_id=1),
                             self.state, self.bot, 222, BASE + timedelta(minutes=3))
        rows = self.state.pending_edits(int((BASE + timedelta(minutes=3)).timestamp()))
        self.assertEqual(len(rows), 4)
        self.assertEqual({r[4] for r in rows}, {'unread', 'unanswered'})

    async def test_operator_same_as_main_gets_only_one_edit(self):
        cfg = dict(self.cfg, main_telegram_id=222)
        msgs = [FakeMessage(1, 90)]
        client = FakeClient(msgs)
        await process_dialog(cfg, OP, client, fake_dialog(msgs, unread_count=1, read_max_id=0),
                             self.state, self.bot, 222, BASE)
        self.assertEqual(len(self.bot.sent), 1)
        await process_dialog(cfg, OP, client, fake_dialog(msgs, unread_count=0, read_max_id=1),
                             self.state, self.bot, 222, BASE + timedelta(minutes=1))
        self.assertEqual(len(self.state.pending_edits(int(BASE.timestamp()) + 60)), 1)

    async def test_disabled_marks_does_not_store_messages(self):
        cfg = dict(self.cfg, edit_resolved_alerts=False)
        msgs = [FakeMessage(1, 90)]
        client = FakeClient(msgs)
        await process_dialog(cfg, OP, client, fake_dialog(msgs, unread_count=1, read_max_id=0),
                             self.state, self.bot, 222, BASE)
        await process_dialog(cfg, OP, client, fake_dialog(msgs, unread_count=0, read_max_id=1),
                             self.state, self.bot, 222, BASE + timedelta(minutes=1))
        self.assertEqual(len(self.bot.sent), 2)
        self.assertEqual(self.state.pending_edits(int(BASE.timestamp()) + 60), [])

    async def test_retry_edits_and_gone_message(self):
        self.state.record_alert('ivan', 333, 'unread', 1, 111, 101, '<b>Alert</b>\nЛид: Пример')
        self.state.mark_resolved('ivan', 333, 'unread', 1, int(BASE.timestamp()))
        self.bot.edit_response = ("retry", None)
        now = int(BASE.timestamp())
        self.assertEqual(await apply_pending_edits(
            self.state, self.bot, self.cfg, pace_seconds=0, now_ts=now), 0)
        self.assertEqual(self.state.pending_edits(now), [])
        self.assertEqual(len(self.state.pending_edits(now+30)), 1)
        self.bot.edit_response = ("gone", None)
        self.assertEqual(await apply_pending_edits(
            self.state, self.bot, self.cfg, pace_seconds=0, now_ts=now+30), 1)
        self.assertEqual(self.state.pending_edits(now+999), [])

    async def test_429_respects_wait_and_pauses_batch(self):
        now = int(BASE.timestamp())
        for mid in (101, 102):
            self.state.record_alert('ivan', 333, 'unread', 1, 111, mid, 'Alert')
        self.state.mark_resolved('ivan', 333, 'unread', 1, now)
        self.bot.edit_response = ("rate_limited", 80)
        self.assertEqual(await apply_pending_edits(
            self.state, self.bot, self.cfg, pace_seconds=0, now_ts=now), 0)
        self.assertEqual(len(self.bot.edits), 1)
        self.assertEqual(len(self.state.pending_edits(now)), 1)
        self.assertEqual(await apply_pending_edits(
            self.state, self.bot, self.cfg, pace_seconds=0, now_ts=now + 10), 0)
        self.assertEqual(len(self.bot.edits), 1)
        self.assertEqual(len(self.state.pending_edits(now+80)), 2)

    def test_legacy_v05_db_keeps_state_after_upgrade(self):
        self.state.close()
        self.tmp.cleanup()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'old.db'
            conn = sqlite3.connect(path)
            conn.execute('''CREATE TABLE tracked_chats_v2 (
                session TEXT NOT NULL, chat_id INTEGER NOT NULL,
                last_seen_id INTEGER NOT NULL DEFAULT 0, first_unanswered_id INTEGER,
                first_unanswered_ts INTEGER, last_completed_ts INTEGER,
                reminders_sent INTEGER NOT NULL DEFAULT 0,
                pending_main INTEGER NOT NULL DEFAULT 0,
                pending_operator INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (session, chat_id))''')
            conn.execute("INSERT INTO tracked_chats_v2 VALUES ('ivan', 333, 5, 1, 10, 20, 3, 0, 0)")
            conn.commit()
            conn.close()
            migrated = State(path)
            self.assertEqual(migrated.get('ivan', 333).reminders_sent, 3)
            self.assertEqual(migrated.pending_edits(1000), [])
            migrated.close()
        # Avoid tearDown double-close of the original State and temp dir.
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / 'state.db')


class BotApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracked_send_and_edit_bot_api(self):
        class Bot(Notifier):
            async def request(self, method, data=None):
                self.calls.append((method, data))
                if method == 'sendMessage':
                    return {'ok': True, 'result': {'message_id': 47}}
                if method == 'editMessageText':
                    return {'ok': True, 'result': {}}
        bot = Bot({'notifier_bot_token': 'test'})
        bot.calls = []
        self.assertEqual(await bot.send_tracked(111, 'hello'), (True, 47))
        self.assertEqual(await bot.edit(111, 47, '✅ hello'), ('edited', None))
        self.assertEqual(bot.calls[1][1]['message_id'], 47)
        self.assertEqual(bot.calls[1][1]['parse_mode'], 'HTML')

    async def test_edit_telegram_errors(self):
        class Bot(Notifier):
            async def request(self, method, data=None):
                return self.reply
        bot = Bot({'notifier_bot_token': 'test'})
        bot.reply = {'ok': False, 'error_code': 429,
                     'description': 'Too Many Requests', 'parameters': {'retry_after': 9}}
        self.assertEqual(await bot.edit(111, 47, 'abc'), ('rate_limited', 10))
        bot.reply = {'ok': False, 'error_code': 400,
                     'description': 'Bad Request: message is not modified'}
        self.assertEqual(await bot.edit(111, 47, 'abc'), ('edited', None))
        bot.reply['description'] = 'Bad Request: message to edit not found'
        self.assertEqual(await bot.edit(111, 47, 'abc'), ('gone', None))


class FormattingTests(unittest.TestCase):
    def test_snapshot_labels_and_utc3(self):
        result = resolved_text('⚠️ <b>Лид не прочитан вовремя</b>\n'
                               'Сейчас: 10:00\nНе прочитано уже: 2 часа\nНепрочитано: 3\n'
                               'Лид: <b>Иван &amp; сын</b>', 'unread', int(BASE.timestamp()), 3)
        self.assertIn('10:00:00 (UTC+3)', result)
        self.assertIn('Было непрочитано: 3', result)
        self.assertIn('Лид: <b>Иван &amp; сын</b>', result)
        self.assertNotIn('Сейчас:', result)

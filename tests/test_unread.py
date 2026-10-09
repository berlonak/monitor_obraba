# copyright by berlonak
# telegram: @Kilax123
"""Regression tests for the v0.4 unread clock, no needless history, and flood backoff."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import yaml
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sla.config import load_config
from sla.monitor import process_dialog, RequestPacer, monitor_session
from sla.state import State
from test_monitor import BASE, FakeMessage, FakeClient, FakeNotifier, LEAD, OP, fake_cfg, fake_dialog


class UnreadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = str(Path(self.tmp.name) / "old.sqlite")
        self.state = State(self.state_file)
        self.notify = FakeNotifier()
        self.cfg = fake_cfg(alert_mode="unread")

    async def asyncTearDown(self):
        self.state.close()
        self.tmp.cleanup()

    async def test_read_dialog_does_not_request_history(self):
        msgs = [FakeMessage(1, 120), FakeMessage(2, 5, out=True)]
        client = FakeClient(msgs)
        await process_dialog(self.cfg, OP, client, fake_dialog(msgs, unread_count=0, read_max_id=1),
                             self.state, self.notify, 222, BASE)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.notify.deliveries, [])

    async def test_oldest_unread_does_not_reset_on_new_lead_messages(self):
        msgs = [FakeMessage(1, 180, out=True), FakeMessage(2, 90), FakeMessage(3, 10)]
        client = FakeClient(msgs)
        await process_dialog(self.cfg, OP, client, fake_dialog(msgs, unread_count=2, read_max_id=1),
                             self.state, self.notify, 222, BASE)
        s = self.state.get_unread('ivan', 333)
        self.assertEqual(s.first_unread_id, 2)
        self.assertEqual(s.first_unread_ts, int(msgs[1].date.timestamp()))
        self.assertEqual(len(self.notify.deliveries), 2)
        self.assertIn('Лид не прочитан вовремя', self.notify.deliveries[0][1])
        self.assertIn('1 ч 30 мин', self.notify.deliveries[0][1])
        # Check new incoming without resetting first timestamp or rereading full history.
        client.messages.append(FakeMessage(4, 1))
        await process_dialog(self.cfg, OP, client,
                             fake_dialog(client.messages, unread_count=3, read_max_id=1),
                             self.state, self.notify, 222, BASE)
        self.assertEqual(self.state.get_unread('ivan', 333).first_unread_id, 2)
        self.assertEqual(len(client.calls), 1)

    async def test_mark_read_without_reply_stops_unread_reminders(self):
        msgs = [FakeMessage(1, 90)]
        client = FakeClient(msgs)
        await process_dialog(self.cfg, OP, client, fake_dialog(msgs, unread_count=1, read_max_id=0),
                             self.state, self.notify, 222, BASE)
        self.assertEqual(len(self.notify.deliveries), 2)
        await process_dialog(self.cfg, OP, client, fake_dialog(msgs, unread_count=0, read_max_id=1),
                             self.state, self.notify, 222, BASE + timedelta(minutes=10))
        s = self.state.get_unread('ivan', 333)
        self.assertIsNone(s.first_unread_id)
        self.assertIsNone(s.last_completed_ts)
        self.assertEqual(len(self.notify.deliveries), 2)
        self.assertEqual(len(client.calls), 1)

    async def test_outgoing_message_does_not_itself_clear_unread_flag(self):
        msgs = [FakeMessage(1, 90), FakeMessage(2, 2, out=True)]
        client = FakeClient(msgs)
        await process_dialog(self.cfg, OP, client, fake_dialog(msgs, unread_count=1, read_max_id=0),
                             self.state, self.notify, 222, BASE)
        self.assertEqual(self.state.get_unread('ivan', 333).first_unread_id, 1)
        self.assertEqual(len(self.notify.deliveries), 2)

    async def test_partial_read_starts_fresh_unread_clock(self):
        msgs = [FakeMessage(1, 95), FakeMessage(2, 85), FakeMessage(3, 10)]
        client = FakeClient(msgs)
        await process_dialog(self.cfg, OP, client, fake_dialog(msgs, unread_count=3, read_max_id=0),
                             self.state, self.notify, 222, BASE)
        self.assertEqual(self.state.get_unread('ivan', 333).first_unread_id, 1)
        self.assertEqual(len(self.notify.deliveries), 2)
        await process_dialog(self.cfg, OP, client, fake_dialog(msgs, unread_count=1, read_max_id=2),
                             self.state, self.notify, 222, BASE)
        s = self.state.get_unread('ivan', 333)
        self.assertEqual(s.first_unread_id, 3)
        self.assertIsNone(s.last_completed_ts)
        self.assertEqual(len(self.notify.deliveries), 2)
        await process_dialog(self.cfg, OP, client, fake_dialog(msgs, unread_count=1, read_max_id=2),
                             self.state, self.notify, 222, BASE + timedelta(minutes=55))
        self.assertEqual(len(self.notify.deliveries), 4)

    async def test_existing_unread_skipped_then_new_incoming_only(self):
        self.cfg['alert_existing_chats_on_start'] = False
        msgs = [FakeMessage(1, 100)]
        client = FakeClient(msgs)
        await process_dialog(self.cfg, OP, client, fake_dialog(msgs, unread_count=1, read_max_id=0),
                             self.state, self.notify, 222, BASE)
        self.assertEqual(len(self.notify.deliveries), 0)
        self.assertEqual(client.calls, [])
        self.state.close()
        self.state = State(self.state_file)
        await process_dialog(self.cfg, OP, client, fake_dialog(msgs, unread_count=1, read_max_id=0),
                             self.state, self.notify, 222, BASE + timedelta(hours=1))
        self.assertEqual(len(self.notify.deliveries), 0)
        client.messages.append(FakeMessage(2, 65))
        await process_dialog(self.cfg, OP, client,
                             fake_dialog(client.messages, unread_count=2, read_max_id=0),
                             self.state, self.notify, 222, BASE)
        self.assertEqual(self.state.get_unread('ivan', 333).first_unread_id, 2)
        self.assertEqual(len(self.notify.deliveries), 2)

    async def test_unread_night_suppression_and_persistence(self):
        from sla.config import parse_quiet_hours
        self.cfg['quiet_window'] = parse_quiet_hours({'start': '23:00', 'end': '09:00'})
        base = datetime(2026, 9, 24, 5, 30, tzinfo=timezone.utc)
        msgs = [FakeMessage(1, 120, base=base)]
        client = FakeClient(msgs)
        dialog = fake_dialog(msgs, unread_count=1, read_max_id=0)
        await process_dialog(self.cfg, OP, client, dialog, self.state, self.notify, 222, base)
        self.state.close()
        self.state = State(self.state_file)
        self.assertEqual(self.notify.deliveries, [])
        wake = datetime(2026, 9, 24, 6, tzinfo=timezone.utc)
        await process_dialog(self.cfg, OP, client, dialog, self.state, self.notify, 222, wake)
        self.assertEqual(len(self.notify.deliveries), 2)
        await process_dialog(self.cfg, OP, client, dialog, self.state, self.notify, 222,
                             wake + timedelta(minutes=10))
        self.assertEqual(len(self.notify.deliveries), 4)

    async def test_both_tracks_two_independent_alerts(self):
        cfg = fake_cfg(alert_mode="both")
        msgs = [FakeMessage(1, 100)]
        client = FakeClient(msgs)
        await process_dialog(cfg, OP, client, fake_dialog(msgs, unread_count=1, read_max_id=0),
                             self.state, self.notify, 222, BASE)
        self.assertEqual(len(self.notify.deliveries), 4)
        headings = [msg for _, msg in self.notify.deliveries]
        self.assertTrue(any('Просрочен ответ' in msg for msg in headings))
        self.assertTrue(any('Лид не прочитан' in msg for msg in headings))
        # Mark read, but remain unanswered: unread stops, unanswered repeats.
        await process_dialog(cfg, OP, client, fake_dialog(msgs, unread_count=0, read_max_id=1),
                             self.state, self.notify, 222, BASE + timedelta(minutes=10))
        self.assertEqual(len(self.notify.deliveries), 6)
        self.assertEqual(self.state.get_unread('ivan', 333).last_completed_ts, None)
        self.assertEqual(self.state.get('ivan', 333).reminders_sent, 1)

    async def test_unread_recipient_retry_survives_restart(self):
        msgs = [FakeMessage(1, 90)]
        client = FakeClient(msgs)
        notify = FakeNotifier(failed=[111])
        dialog = fake_dialog(msgs, unread_count=1, read_max_id=0)
        await process_dialog(self.cfg, OP, client, dialog, self.state, notify, 222, BASE)
        self.assertTrue(self.state.get_unread('ivan', 333).pending_main)
        self.state.close()
        self.state = State(self.state_file)
        notify.failed.clear()
        await process_dialog(self.cfg, OP, client, dialog, self.state, notify, 222, BASE + timedelta(minutes=1))
        self.assertEqual([x[0] for x in notify.deliveries], [222, 111])

    async def test_missing_read_marker_pauses_until_server_supplies_it(self):
        msgs = [FakeMessage(1, 95), FakeMessage(2, 85)]
        client = FakeClient(msgs)
        # The count alone does not distinguish an actual unread message from
        # a manually re-marked chat if the server marker is unavailable.
        await process_dialog(self.cfg, OP, client, fake_dialog(msgs, unread_count=2),
                             self.state, self.notify, 222, BASE)
        self.assertIsNone(self.state.get_unread('ivan', 333).first_unread_id)
        self.assertEqual(self.notify.deliveries, [])
        self.assertEqual(client.calls, [])
        # Same top message and count, but the first reliable snapshot must
        # trigger a history lookup rather than being suppressed as duplicate.
        await process_dialog(self.cfg, OP, client,
                             fake_dialog(msgs, unread_count=2, read_max_id=0),
                             self.state, self.notify, 222, BASE)
        self.assertEqual(self.state.get_unread('ivan', 333).first_unread_id, 1)
        self.assertTrue(self.state.get_unread('ivan', 333).read_marker_known)
        self.assertEqual(len(self.notify.deliveries), 2)

    async def test_no_match_scan_is_not_repeated_every_poll(self):
        cfg = fake_cfg(alert_mode='unread', alert_existing_chats_on_start=False)
        msgs = [FakeMessage(1, 100)]
        client = FakeClient(msgs)
        await process_dialog(cfg, OP, client, fake_dialog(msgs, unread_count=1, read_max_id=0),
                             self.state, self.notify, 222, BASE)
        client.messages.append(FakeMessage(2, 5, out=True))
        dialog = fake_dialog(client.messages, unread_count=1, read_max_id=0)
        await process_dialog(cfg, OP, client, dialog, self.state, self.notify, 222, BASE)
        self.assertEqual(len(client.calls), 1)
        await process_dialog(cfg, OP, client, dialog, self.state, self.notify, 222, BASE)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.notify.deliveries, [])

    async def test_pacing_spacing(self):
        pacer = RequestPacer(.025)
        loop = asyncio.get_running_loop()
        start = loop.time()
        await pacer.wait()
        await pacer.wait()
        self.assertGreaterEqual(loop.time() - start, .020)


class FloodTests(unittest.IsolatedAsyncioTestCase):
    async def test_floodwait_aborts_sweep_and_waits_requested_time(self):
        class FloodWaitError(Exception):
            def __init__(self, seconds):
                self.seconds = seconds

        class FloodClient:
            scans = 0
            def __init__(self, *args):
                self.flood_sleep_threshold = 60
            async def connect(self): pass
            async def is_user_authorized(self): return True
            async def get_me(self): return SimpleNamespace(id=222)
            async def disconnect(self): pass
            async def iter_dialogs(self, archived):
                type(self).scans += 1
                raise FloodWaitError(7)
                yield None

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'operator.session'
            path.touch()
            cfg = dict(fake_cfg(), api_id=123, api_hash='a' * 32, include_archived=False,
                       flood_wait_extra_seconds=3, history_request_delay_seconds=0)
            sess = dict(OP, session_file=str(path.with_suffix('')))
            sleeps = []
            async def stop_on_sleep(seconds):
                sleeps.append(seconds)
                raise asyncio.CancelledError
            fake_tel = SimpleNamespace(TelegramClient=FloodClient,
                                       errors=SimpleNamespace(FloodWaitError=FloodWaitError))
            st = State(Path(td) / 'state.db')
            try:
                with patch.dict(sys.modules, {'telethon': fake_tel}), patch('sla.monitor.asyncio.sleep', stop_on_sleep):
                    with self.assertRaises(asyncio.CancelledError):
                        await monitor_session(cfg, sess, st, FakeNotifier())
                self.assertEqual(FloodClient.scans, 1)
                self.assertEqual(sleeps, [10])
            finally:
                st.close()

class NewConfigTests(unittest.TestCase):
    def test_new_mode_and_flood_controls_validate(self):
        obj = {
            'api_id': 123, 'api_hash': 'a' * 32,
            'notifier_bot_token': '12345678:' + 'A' * 30,
            'main_telegram_id': 111,
            'sessions': [{'name':'op', 'operator_label':'Оператор',
                          'operator_telegram_id': 222, 'session_file':'sessions/op'}],
        }
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'config.yaml'
            def parse(d):
                p.write_text(yaml.safe_dump(d, allow_unicode=True), encoding='utf8')
                return load_config(p)
            self.assertEqual(parse(obj)['alert_mode'], 'unanswered')
            for mode in ('unanswered', 'unread', 'both'):
                self.assertEqual(parse(dict(obj, alert_mode=mode))['alert_mode'], mode)
            with self.assertRaisesRegex(ValueError, 'alert_mode'):
                parse(dict(obj, alert_mode='broken'))
            for field in ('history_request_delay_seconds', 'history_page_delay_seconds'):
                for value in (-1, '3', True, 90):
                    with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                        parse(dict(obj, **{field:value}))
            self.assertEqual(parse(dict(obj, history_request_delay_seconds=2.5))[
                'history_request_delay_seconds'], 2.5)

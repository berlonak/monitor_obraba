# copyright by berlonak
# telegram: @Kilax123
"""Debug settings, secret redaction and read-event regression tests."""
from datetime import timedelta
import logging
from pathlib import Path
import tempfile
import unittest
import yaml

from sla.config import load_config
from sla.debuglog import configure_logging
from sla.monitor import apply_read_update, process_dialog
from sla.resolutions import apply_pending_edits
from sla.state import State
from test_monitor import BASE, FakeClient, FakeMessage, OP, fake_cfg, fake_dialog
from test_resolutions import TrackedBot


class DiagnosticConfigTests(unittest.TestCase):
    def make_config(self, dirname, **kwargs):
        cfg = dict(api_id=123456, api_hash='a'*32, notifier_bot_token='1234:ABCDEFGHIJKLMNOPQRSTUVWXYZ01',
                   main_telegram_id=111, sessions=[{'name': 'ivan', 'operator_label': 'Ivan',
                   'operator_telegram_id': 222, 'session_file': 'sessions/ivan'}], **kwargs)
        dest = Path(dirname) / 'config.yaml'
        dest.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding='utf-8')
        return dest

    def test_debug_file_relative_to_config_and_defaults_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_config(tmp)
            off = load_config(path)
            self.assertFalse(off['debug_logging'])
            self.assertEqual(Path(off['debug_log_file']), Path(tmp)/'logs'/'sla_debug.log')
            path = self.make_config(tmp, debug_logging=True, debug_log_file='trace/mydebug.log')
            on = load_config(path)
            self.assertTrue(on['debug_logging'])
            self.assertEqual(Path(on['debug_log_file']), Path(tmp)/'trace'/'mydebug.log')

    def test_invalid_debug_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'debug_logging'):
                load_config(self.make_config(tmp, debug_logging='true'))
            with self.assertRaisesRegex(ValueError, 'debug_log_file'):
                load_config(self.make_config(tmp, debug_log_file=''))

    def test_debug_file_contains_decisions_but_no_api_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(self.make_config(tmp, debug_logging=True))
            # During test, restore log handlers on exit so other tests stay clean.
            root = logging.getLogger()
            old_handlers = root.handlers[:]
            old_level = root.level
            root.handlers = []
            try:
                configure_logging(cfg)
                logging.getLogger('sla.monitor').debug('chat=33 marker=44 secret=%s', cfg['api_hash'])
                logging.getLogger('sla.notifier').error('bad URL https://api.telegram.org/bot1234:ABCDEFGHIJKLMNOPQRSTUVWXYZ01/editMessageText')
                try:
                    raise ValueError('secret 1234:ABCDEFGHIJKLMNOPQRSTUVWXYZ01')
                except ValueError:
                    logging.getLogger('sla.notifier').exception('Ошибка сети')
                for handler in root.handlers:
                    handler.flush()
                logged = Path(cfg['debug_log_file']).read_text(encoding='utf-8')
                self.assertIn('chat=33 marker=44', logged)
                self.assertIn('Ошибка сети', logged)
                self.assertNotIn(cfg['api_hash'], logged)
                self.assertNotIn(cfg['notifier_bot_token'], logged)
            finally:
                for handler in root.handlers:
                    handler.close()
                root.handlers = old_handlers
                root.setLevel(old_level)


class QuickReadUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name)/'state.sqlite3')
        self.bot = TrackedBot()
        self.cfg = fake_cfg(alert_mode='unread', stats_enabled=False, edit_resolved_alerts=True)

    async def asyncTearDown(self):
        self.state.close()
        self.tmp.cleanup()

    async def test_read_event_queues_green_marks_without_waiting_full_pass(self):
        messages = [FakeMessage(1, 100)]
        client = FakeClient(messages)
        await process_dialog(self.cfg, OP, client, fake_dialog(messages, unread_count=1, read_max_id=0),
                             self.state, self.bot, 222, BASE)
        await process_dialog(self.cfg, OP, client, fake_dialog(messages, unread_count=1, read_max_id=0),
                             self.state, self.bot, 222, BASE+timedelta(minutes=15))
        self.assertEqual(len(self.bot.sent), 4)
        first_case = self.state.get_unread(OP['name'], 333).first_unread_id
        self.assertEqual(first_case, 1)
        self.assertTrue(apply_read_update(self.cfg, OP, self.state, 333, 1, int(BASE.timestamp()) + 901))
        self.assertEqual(self.state.get_unread(OP['name'], 333).first_unread_id, None)
        self.assertFalse(apply_read_update(self.cfg, OP, self.state, 333, 1, int(BASE.timestamp()) + 902))
        self.assertEqual(len(self.state.pending_edits(int(BASE.timestamp()) + 903)), 4)
        changed = await apply_pending_edits(self.state, self.bot, self.cfg,
                                            now_ts=int(BASE.timestamp()) + 903, pace_seconds=0)
        self.assertEqual(changed, 4)
        self.assertEqual(len(self.bot.edits), 4)
        self.assertTrue(all(body.startswith('✅') for _, _, body in self.bot.edits))

    async def test_manual_remark_after_read_update_is_not_new_unread(self):
        msg = [FakeMessage(1, 100)]
        client = FakeClient(msg)
        await process_dialog(self.cfg, OP, client, fake_dialog(msg, unread_count=1, read_max_id=0),
                             self.state, self.bot, 222, BASE)
        self.assertTrue(apply_read_update(self.cfg, OP, self.state, 333, 1, int(BASE.timestamp()) + 10))
        # User manually marks chat as unread; server read position stays confirmed.
        manual = fake_dialog(msg, unread_count=0, read_max_id=1)
        manual.dialog.unread_mark = True
        await process_dialog(self.cfg, OP, client, manual, self.state, self.bot, 222, BASE+timedelta(hours=1))
        self.assertIsNone(self.state.get_unread(OP['name'], 333).first_unread_id)
        self.assertEqual(len(self.bot.sent), 2)

    async def test_stale_dialog_does_not_override_fast_read_event(self):
        msg = [FakeMessage(1, 100)]
        client = FakeClient(msg)
        await process_dialog(self.cfg, OP, client, fake_dialog(msg, unread_count=1, read_max_id=0),
                             self.state, self.bot, 222, BASE)
        self.assertTrue(apply_read_update(self.cfg, OP, self.state, 333, 1, int(BASE.timestamp()) + 10))
        # Stale dialog snapshot after read update: should neither reopen nor alert.
        await process_dialog(self.cfg, OP, client, fake_dialog(msg, unread_count=1, read_max_id=0),
                             self.state, self.bot, 222, BASE+timedelta(hours=1))
        self.assertIsNone(self.state.get_unread(OP['name'], 333).first_unread_id)
        self.assertEqual(len(self.bot.sent), 2)

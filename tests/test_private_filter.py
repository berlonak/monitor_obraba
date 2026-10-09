# copyright by berlonak
# telegram: @Kilax123
"""Private-dialog filtering regression tests, fully offline."""
import asyncio
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sla.monitor import dialog_skip_reason, monitor_session, process_dialog
from sla.state import State
from test_monitor import FakeMessage, FakeNotifier, OP, fake_cfg, BASE


def fake_entry(uid, *, private=True, group=False, channel=False, bot=False,
               deleted=False, contact=False, message=True, peer=None):
    entity = SimpleNamespace(id=uid, bot=bot, deleted=deleted, contact=contact)
    entry = SimpleNamespace(id=uid, entity=entity, is_user=private, is_group=group,
                            is_channel=channel, message=FakeMessage(1, 2) if message else None,
                            unread_count=0)
    if peer is not None:
        entry.dialog = SimpleNamespace(peer=peer, read_inbox_max_id=1)
    return entry


class PrivateFilterTests(unittest.TestCase):
    def setUp(self):
        self.cfg = fake_cfg()

    def test_only_genuine_one_to_one_dialogs_are_eligible(self):
        private = fake_entry(333)
        self.assertIsNone(dialog_skip_reason(private, self.cfg, 222))
        cases = [
            (fake_entry(-1, private=False, group=True), 'группы'),
            (fake_entry(-2, private=False, channel=True), 'каналы'),
            (fake_entry(-3, private=False), 'прочие_не_личные'),
            (fake_entry(333, group=True), 'группы'),  # inconsistent flags, fail closed
            (fake_entry(444, bot=True), 'боты'),
            (fake_entry(444, deleted=True), 'удалённые'),
            (fake_entry(222), 'свой_аккаунт'),
            (fake_entry(444, message=False), 'без_сообщений'),
        ]
        for entry, expected in cases:
            with self.subTest(reason=expected):
                self.assertEqual(dialog_skip_reason(entry, self.cfg, 222), expected)

    def test_config_allow_block_and_contacts_apply_before_history(self):
        entry = fake_entry(333)
        self.assertEqual(dialog_skip_reason(entry, fake_cfg(include_ids=[444]), 222),
                         'не_в_include_ids')
        self.assertEqual(dialog_skip_reason(entry, fake_cfg(exclude_ids=[333]), 222),
                         'в_exclude_ids')
        self.assertEqual(dialog_skip_reason(entry, fake_cfg(only_contacts=True), 222),
                         'не_контакт')
        self.assertIsNone(dialog_skip_reason(fake_entry(333, contact=True),
                                             fake_cfg(only_contacts=True), 222))

    def test_raw_peer_must_be_user_with_matching_id(self):
        class PeerUser:
            def __init__(self, user_id): self.user_id = user_id
        class PeerChat:
            def __init__(self, chat_id): self.chat_id = chat_id
        self.assertIsNone(dialog_skip_reason(fake_entry(333, peer=PeerUser(333)),
                                             self.cfg, 222))
        for peer in [PeerUser(444), PeerChat(333)]:
            self.assertEqual(dialog_skip_reason(fake_entry(333, peer=peer),
                                                self.cfg, 222), 'прочие_не_личные')


class MixedSweepTests(unittest.IsolatedAsyncioTestCase):
    async def test_sweep_counts_only_private_chats_and_does_not_fetch_group_history(self):
        class FloodWaitError(Exception):
            def __init__(self, seconds): self.seconds = seconds

        dialogs = [
            fake_entry(-1, private=False, group=True),
            fake_entry(-2, private=False, channel=True),
            fake_entry(333, contact=True),
            fake_entry(444, bot=True),
            fake_entry(555, deleted=True),
            fake_entry(222),
            fake_entry(666, contact=False),
            fake_entry(777, message=False),
        ]

        class MixedClient:
            histories = []
            def __init__(self, *_): self.flood_sleep_threshold = None
            async def connect(self): pass
            async def is_user_authorized(self): return True
            async def get_me(self): return SimpleNamespace(id=222)
            async def disconnect(self): pass
            async def iter_dialogs(self, archived):
                self.archived = archived
                for entry in dialogs:
                    yield entry
            async def iter_messages(self, entity, **kwargs):
                type(self).histories.append(entity.id)
                yield FakeMessage(1, 2)

        with tempfile.TemporaryDirectory() as tempdir:
            session_file = Path(tempdir) / 'operator.session'
            session_file.touch()
            cfg = dict(fake_cfg(alert_mode='unanswered', stats_enabled=False,
                                sla_seconds=999999999, history_request_delay_seconds=0,
                                history_page_delay_seconds=0),
                       api_id=123, api_hash='a'*32, include_archived=False,
                       flood_wait_extra_seconds=3)
            sess = dict(OP, session_file=str(session_file.with_suffix('')))
            state = State(Path(tempdir) / 'state.sqlite3')
            fake_tel = SimpleNamespace(TelegramClient=MixedClient,
                                       errors=SimpleNamespace(FloodWaitError=FloodWaitError))
            async def stop_after_one_pass(_): raise asyncio.CancelledError()
            try:
                with patch.dict(sys.modules, {'telethon': fake_tel}), \
                     patch('sla.monitor.asyncio.sleep', stop_after_one_pass), \
                     self.assertLogs('sla.monitor', logging.INFO) as logs:
                    with self.assertRaises(asyncio.CancelledError):
                        await monitor_session(cfg, sess, state, FakeNotifier())
                self.assertEqual(MixedClient.histories, [333, 666])
                summary = '\n'.join(logs.output)
                self.assertIn('итого получено 8 диалогов', summary)
                self.assertIn('личных подходящих 2, проверено 2', summary)
                self.assertIn('отсеяно 6', summary)
                self.assertIn('группы=1', summary)
                self.assertIn('каналы=1', summary)
                self.assertIn('боты=1', summary)
                self.assertIsNotNone(state.get('ivan', 333))
                self.assertIsNotNone(state.get('ivan', 666))
                self.assertIsNone(state.get('ivan', 444))
            finally:
                state.close()

    async def test_direct_process_call_reuses_filter(self):
        class NoHistoryClient:
            async def iter_messages(self, *_args, **_kwargs):
                raise AssertionError('History should never be fetched')
                yield
        with tempfile.TemporaryDirectory() as td:
            state = State(Path(td)/'state.sqlite3')
            try:
                for bad in [fake_entry(-1, private=False, group=True),
                            fake_entry(555, bot=True), fake_entry(-2, private=False, channel=True)]:
                    await process_dialog(fake_cfg(), OP, NoHistoryClient(), bad,
                                         state, FakeNotifier(), 222, BASE)
            finally:
                state.close()


if __name__ == '__main__':
    unittest.main()

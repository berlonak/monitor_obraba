# copyright by berlonak
# telegram: @Kilax123
"""Offline tests: no Telegram token or Internet access required."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import yaml

from sla.config import load_config, load_login_settings, parse_quiet_hours, resolve_session_path, session_database_path
from sla.monitor import process_dialog, refresh_chat
from sla.state import State
from sla.timeutils import in_quiet_hours


UTC = timezone.utc
BASE = datetime(2026, 9, 24, 7, 0, tzinfo=UTC)  # 10:00 UTC+3


class FakeMessage:
    def __init__(self, mid, minutes_ago, out=False, text="hello", base=BASE):
        self.id = mid
        self.date = base - timedelta(minutes=minutes_ago)
        self.out = out
        self.message = text
        self.media = None


class FakeClient:
    def __init__(self, messages):
        self.messages = list(messages)
        self.calls = []

    async def iter_messages(self, entity, limit=None, min_id=0, reverse=False, wait_time=None):
        self.calls.append((limit, min_id, reverse, wait_time))
        matches = sorted((m for m in self.messages if m.id > min_id),
                         key=lambda m: m.id, reverse=not reverse)
        for message in matches[:limit]:
            yield message


class FakeNotifier:
    def __init__(self, failed=None):
        self.failed = set(failed or [])
        self.deliveries = []
        self.attempts = []

    async def send(self, uid, message):
        self.attempts.append(uid)
        if uid in self.failed:
            return False
        self.deliveries.append((uid, message))
        return True


def fake_cfg(**overrides):
    cfg = {
        "main_telegram_id": 111,
        "notify_operator": True,
        "stats_enabled": False,
        "sla_seconds": 3600,
        "remind_every_seconds": 600,
        "max_reminders": 0,
        "poll_interval_seconds": 60,
        "utc_offset_hours": 3,
        "quiet_window": None,
        "include_ids": [],
        "exclude_ids": [],
        "only_contacts": False,
        "message_preview_len": 160,
        "history_scan_limit": 500,
        "alert_existing_chats_on_start": True,
    }
    cfg.update(overrides)
    return cfg


OP = {"name": "ivan", "operator_label": "Иван", "operator_telegram_id": 222}


def standalone_test_config():
    """A deterministic fixture: tests must not depend on edited config.example.yaml."""
    return {
        "api_id": 12345,
        "api_hash": "a" * 32,
        "notifier_bot_token": "12345678:" + "A" * 30,
        "main_telegram_id": 111111111,
        "login_session_name": "sessions/operator_ivan",
        "sla_seconds": 3600,
        "remind_every_seconds": 600,
        "quiet_hours": {"start": "23:00", "end": "09:00"},
        "sessions": [{
            "name": "ivan", "operator_label": "Иван",
            "operator_telegram_id": 222222222,
            "session_file": "sessions/operator_ivan",
        }],
    }


LEAD = SimpleNamespace(id=333, first_name="Сергей", last_name="Т", username="test_name", bot=False, deleted=False, contact=False)


def fake_dialog(messages, unread_count=3, read_max_id=None):
    obj = SimpleNamespace(is_user=True, entity=LEAD, message=max(messages, key=lambda m: m.id),
                          unread_count=unread_count)
    if read_max_id is not None:
        obj.dialog = SimpleNamespace(read_inbox_max_id=read_max_id)
    return obj


class ConfigTests(unittest.TestCase):
    def test_quiet_hours_across_midnight(self):
        window = parse_quiet_hours({"start": "23:00", "end": "09:00"})
        self.assertTrue(in_quiet_hours(datetime(2026, 9, 23, 20, 0, tzinfo=UTC), window, 3))
        self.assertTrue(in_quiet_hours(datetime(2026, 9, 24, 5, 59, tzinfo=UTC), window, 3))
        self.assertFalse(in_quiet_hours(datetime(2026, 9, 24, 6, 0, tzinfo=UTC), window, 3))
        self.assertFalse(in_quiet_hours(datetime(2026, 9, 24, 19, 59, tzinfo=UTC), window, 3))

    def test_zero_disables_sleep_and_equal_time_invalid(self):
        self.assertIsNone(parse_quiet_hours(0))
        self.assertFalse(in_quiet_hours(BASE, None, 3))
        for bad in ("0", {"start": "9:00", "end": "12:00"}, {"start": "00:00", "end": "00:00"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_quiet_hours(bad)

    def test_daytime_window(self):
        w = parse_quiet_hours({"start": "12:00", "end": "13:00"})
        self.assertTrue(in_quiet_hours(datetime(2026, 9, 24, 9, 30, tzinfo=UTC), w, 3))
        self.assertFalse(in_quiet_hours(BASE, w, 3))

    def test_sample_valid_after_patching_secrets_and_explicit_operator_id(self):
        # Validate whatever the user currently has in the sample, not a fixed
        # personal Telegram ID, specific session basename or reminder interval.
        sample = Path(__file__).resolve().parents[1] / "config.example.yaml"
        obj = yaml.safe_load(sample.read_text(encoding="utf-8"))
        obj["api_hash"] = "a" * 32
        obj["notifier_bot_token"] = "12345678:" + "A" * 30
        obj["notify_operator"] = True
        first = obj["sessions"][0]
        expected_id = first.get("operator_telegram_id", first.get("operator_id"))
        self.assertIsInstance(expected_id, int)
        expected_file = first["session_file"]
        expected_login = obj.get("login_session_name")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.yaml"
            path.write_text(yaml.safe_dump(obj, allow_unicode=True), encoding="utf-8")
            cfg = load_config(path)
            self.assertEqual(cfg["sessions"][0]["operator_telegram_id"], expected_id)
            self.assertEqual(cfg["sessions"][0]["session_file"],
                             resolve_session_path(expected_file, td, "session_file"))
            if expected_login is not None:
                self.assertEqual(cfg["login_session_name"],
                                 resolve_session_path(expected_login, td, "login_session_name"))
            self.assertEqual(cfg["sla_seconds"], obj.get("sla_seconds", 3600))
            self.assertEqual(cfg["remind_every_seconds"], obj.get("remind_every_seconds", 600))
            self.assertTrue(cfg["state_db"].startswith(td))
            # Independently test the 0 (= no quiet hours) feature.
            obj["quiet_hours"] = 0
            path.write_text(yaml.safe_dump(obj, allow_unicode=True), encoding="utf-8")
            self.assertIsNone(load_config(path)["quiet_window"])
            # With notify_operator=true, an ID is mandatory.
            first.pop("operator_telegram_id", None)
            first.pop("operator_id", None)
            path.write_text(yaml.safe_dump(obj, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "operator_telegram_id"):
                load_config(path)
            first["operator_id"] = expected_id
            path.write_text(yaml.safe_dump(obj, allow_unicode=True), encoding="utf-8")
            self.assertEqual(load_config(path)["sessions"][0]["operator_telegram_id"], expected_id)


class LoginCompatibilityTests(unittest.TestCase):
    def test_original_login_file_is_bit_for_bit_identical(self):
        import hashlib
        root = Path(__file__).resolve().parents[1]
        login_script = root / "login_telethon.py"
        if not login_script.is_file():
            self.skipTest("Оригинальная логинилка не скопирована в папку проекта")
        raw = login_script.read_bytes()
        # SHA256 of the exact user-provided login_telethon.py.
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         "07edc1fc82d03669ca7bc9b7eaec4174abcb6e18ae04519fe73b7895acc7be70")

    def test_login_adapter_and_original_script_use_same_path_as_monitor(self):
        import asyncio
        import contextlib
        import importlib.util
        import io
        import os
        import runpy
        import sys
        from unittest.mock import patch

        root = Path(__file__).resolve().parents[1]
        if not (root / "login_telethon.py").is_file():
            self.skipTest("Для проверки реального файла нужна оригинальная login_telethon.py")
        template = standalone_test_config()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.yaml"
            path.write_text(yaml.safe_dump(template, allow_unicode=True), encoding="utf-8")
            api_id, api_hash, login_name = load_login_settings(path)
            cfg = load_config(path)
            self.assertEqual(api_id, cfg["api_id"])
            self.assertEqual(api_hash, cfg["api_hash"])
            self.assertEqual(login_name, cfg["sessions"][0]["session_file"])
            self.assertTrue(session_database_path(login_name).parent.is_dir())

            # Import exactly the public root-level config module from the
            # unchanged login script, with test-specific YAML settings.
            with patch.dict(os.environ, {"SLA_CONFIG": str(path)}):
                spec = importlib.util.spec_from_file_location("login_adapter_test", root / "config.py")
                adapter = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(adapter)

            self.assertEqual(adapter.API_ID, cfg["api_id"])
            self.assertEqual(adapter.API_HASH, cfg["api_hash"])
            self.assertEqual(adapter.TELETHON_SESSION_NAME, cfg["sessions"][0]["session_file"])

            # Execute the untouched user's script in an entirely offline stub
            # and verify its TelegramClient gets the same file path.
            seen = []

            class FakeTelegramClient:
                def __init__(self, file, api_id_, api_hash_):
                    seen.append((file, api_id_, api_hash_))
                    self.loop = asyncio.new_event_loop()

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_val, exc_tb):
                    self.loop.close()

                async def start(self):
                    pass

                async def get_me(self):
                    return SimpleNamespace(id=222222222, username="operator_ivan")

            stdout = io.StringIO()
            with patch.dict(sys.modules, {"telethon": SimpleNamespace(TelegramClient=FakeTelegramClient),
                                          "config": adapter}):
                with contextlib.redirect_stdout(stdout):
                    runpy.run_path(str(root / "login_telethon.py"), run_name="__main__")
            self.assertEqual(seen, [(login_name, api_id, api_hash)])
            self.assertIn("OK: @operator_ivan id=222222222", stdout.getvalue())

    def test_login_works_without_configuring_notification_bot_or_operators(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.yaml"
            path.write_text(yaml.safe_dump({"api_id": 12345, "api_hash": "a" * 32,
                                            "login_session_name": "sessions/new_operator"}), encoding="utf-8")
            self.assertEqual(load_login_settings(path)[2], str(Path(td) / "sessions/new_operator"))
            self.assertTrue((Path(td) / "sessions").is_dir())

    def test_multiple_operators_require_different_session_files(self):
        template = standalone_test_config()
        template["sessions"].append({"name": "petr", "operator_label": "Пётр",
                                     "operator_telegram_id": 333333333,
                                     "session_file": "sessions/operator_ivan.session"})
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.yaml"
            path.write_text(yaml.safe_dump(template, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "одна файловая сессия назначена двум"):
                load_config(path)


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = str(Path(self.tmp.name) / "test.sqlite")
        self.state = State(self.state_path)

    async def asyncTearDown(self):
        self.state.close()
        self.tmp.cleanup()

    async def test_monitor_passes_same_file_path_to_telethon(self):
        import sys
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sessions" / "ivan.session"
            path.parent.mkdir(parents=True)
            path.touch()
            seen = []

            class FakeTelegramClient:
                def __init__(self, filename, api_id, api_hash):
                    seen.append((filename, api_id, api_hash))

                async def connect(self):
                    pass

                async def is_user_authorized(self):
                    return False

                async def disconnect(self):
                    pass

            from sla.monitor import monitor_session
            fake_tel = SimpleNamespace(TelegramClient=FakeTelegramClient, errors=SimpleNamespace(FloodWaitError=Exception))
            cfg = {"api_id": 12345, "api_hash": "a" * 32}
            session = {"name": "ivan", "session_file": str(path.with_suffix("")),
                       "operator_telegram_id": 222}
            with patch.dict(sys.modules, {"telethon": fake_tel}):
                await monitor_session(cfg, session, self.state, FakeNotifier())
            self.assertEqual(seen, [(str(path.with_suffix("")), 12345, "a" * 32)])

    async def test_oldest_unanswered_does_not_reset_on_followup(self):
        messages = [FakeMessage(1, 180, out=True), FakeMessage(2, 90), FakeMessage(3, 20)]
        client = FakeClient(messages)
        notifier = FakeNotifier()
        await process_dialog(fake_cfg(), OP, client, fake_dialog(messages), self.state, notifier, 222, BASE)
        s = self.state.get("ivan", 333)
        self.assertEqual(s.first_unanswered_id, 2)
        self.assertEqual(s.first_unanswered_ts, int(messages[1].date.timestamp()))
        self.assertEqual([i[0] for i in notifier.deliveries], [111, 222])
        self.assertIn("1 ч 30 мин", notifier.deliveries[0][1])
        self.assertEqual(s.last_completed_ts, int(BASE.timestamp()))

    async def test_latest_outgoing_clears_alert_and_new_incoming_restarts(self):
        cfg = fake_cfg()
        messages = [FakeMessage(1, 120), FakeMessage(2, 100)]
        client = FakeClient(messages)
        notifier = FakeNotifier()
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222, BASE)
        self.assertEqual(self.state.get("ivan", 333).first_unanswered_id, 1)
        # Operator replies and then a new request comes back 5 minutes later.
        client.messages.extend([FakeMessage(3, 5, out=True), FakeMessage(4, 2)])
        await process_dialog(cfg, OP, client, fake_dialog(client.messages), self.state, notifier, 222, BASE)
        s = self.state.get("ivan", 333)
        self.assertEqual(s.first_unanswered_id, 4)
        self.assertIsNone(s.last_completed_ts)
        self.assertEqual(len(notifier.deliveries), 2)  # New request has not exceeded SLA.
        # A fresh request becomes overdue, regardless of older already-answered messages.
        await process_dialog(cfg, OP, client, fake_dialog(client.messages), self.state, notifier, 222,
                             BASE + timedelta(minutes=65))
        self.assertEqual(len(notifier.deliveries), 4)

    async def test_quiet_hours_no_alert_then_one_wakeup_alert(self):
        window = parse_quiet_hours({"start": "23:00", "end": "09:00"})
        cfg = fake_cfg(quiet_window=window)
        base = datetime(2026, 9, 24, 5, 30, tzinfo=UTC)  # 08:30 UTC+3
        messages = [FakeMessage(1, 110, base=base)]
        client = FakeClient(messages)
        notifier = FakeNotifier()
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222, base)
        self.assertEqual(notifier.deliveries, [])
        self.assertEqual(self.state.get("ivan", 333).first_unanswered_id, 1)
        wake = datetime(2026, 9, 24, 6, 0, tzinfo=UTC)  # 09:00 UTC+3
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222, wake)
        self.assertEqual(len(notifier.deliveries), 2)
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222, wake)
        self.assertEqual(len(notifier.deliveries), 2)
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222, wake + timedelta(minutes=10))
        self.assertEqual(len(notifier.deliveries), 4)

    async def test_service_message_does_not_count_as_operator_reply(self):
        cfg = fake_cfg()
        messages = [FakeMessage(1, 90), FakeMessage(2, 1, out=True, text="")]
        client = FakeClient(messages)
        notifier = FakeNotifier()
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222, BASE)
        self.assertEqual(self.state.get("ivan", 333).first_unanswered_id, 1)
        self.assertEqual(len(notifier.deliveries), 2)

    async def test_quiet_hours_defer_a_due_reminder_without_catchup_burst(self):
        cfg = fake_cfg(quiet_window=parse_quiet_hours({"start": "23:00", "end": "09:00"}))
        day = datetime(2026, 9, 23, 19, 0, tzinfo=UTC)  # 22:00 UTC+3
        messages = [FakeMessage(1, 65, base=day)]
        client = FakeClient(messages)
        notifier = FakeNotifier()
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222, day)
        self.assertEqual(len(notifier.deliveries), 2)
        # Reminders are due at night but do not go out then.
        night = datetime(2026, 9, 23, 21, 0, tzinfo=UTC)  # 00:00 UTC+3
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222, night)
        self.assertEqual(len(notifier.deliveries), 2)
        # One reminder after wakeup. Missed night repeats are not queued.
        wake = datetime(2026, 9, 24, 6, 0, tzinfo=UTC)   # 09:00 UTC+3
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222, wake)
        self.assertEqual(len(notifier.deliveries), 4)
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222, wake)
        self.assertEqual(len(notifier.deliveries), 4)
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222,
                             wake + timedelta(minutes=10))
        self.assertEqual(len(notifier.deliveries), 6)

    async def test_respond_at_night_means_no_wakeup_notification(self):
        window = parse_quiet_hours({"start": "23:00", "end": "09:00"})
        cfg = fake_cfg(quiet_window=window)
        base = datetime(2026, 9, 24, 5, 30, tzinfo=UTC)
        messages = [FakeMessage(1, 120, base=base)]
        client = FakeClient(messages)
        notifier = FakeNotifier()
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222, base)
        client.messages.append(FakeMessage(2, 1, out=True, base=base))
        await process_dialog(cfg, OP, client, fake_dialog(client.messages), self.state, notifier, 222, base)
        await process_dialog(cfg, OP, client, fake_dialog(client.messages), self.state, notifier, 222,
                             datetime(2026, 9, 24, 6, 0, tzinfo=UTC))
        self.assertEqual(notifier.deliveries, [])

    async def test_failed_receiver_is_retried_without_duplicate_to_successful_one(self):
        cfg = fake_cfg()
        messages = [FakeMessage(1, 85)]
        notifier = FakeNotifier(failed=[111])
        client = FakeClient(messages)
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222, BASE)
        s = self.state.get("ivan", 333)
        self.assertTrue(s.pending_main)
        self.assertFalse(s.pending_operator)
        self.assertEqual([item[0] for item in notifier.deliveries], [222])
        notifier.failed.clear()
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222,
                             BASE + timedelta(minutes=1))
        self.assertEqual([item[0] for item in notifier.deliveries], [222, 111])
        self.assertIsNotNone(self.state.get("ivan", 333).last_completed_ts)
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222,
                             BASE + timedelta(minutes=11))
        self.assertEqual([item[0] for item in notifier.deliveries], [222, 111, 111, 222])

    async def test_repeated_alerts_unlimited_persist_restart_and_limit_optional(self):
        cfg = fake_cfg(max_reminders=0)
        messages = [FakeMessage(1, 85)]
        client = FakeClient(messages)
        notifier = FakeNotifier()
        dialog = fake_dialog(messages)
        await process_dialog(cfg, OP, client, dialog, self.state, notifier, 222, BASE)
        self.state.close()
        self.state = State(self.state_path)
        await process_dialog(cfg, OP, client, dialog, self.state, notifier, 222, BASE + timedelta(minutes=9))
        self.assertEqual(len(notifier.deliveries), 2)
        await process_dialog(cfg, OP, client, dialog, self.state, notifier, 222, BASE + timedelta(minutes=10))
        self.assertEqual(len(notifier.deliveries), 4)
        await process_dialog(cfg, OP, client, dialog, self.state, notifier, 222, BASE + timedelta(minutes=20))
        self.assertEqual(len(notifier.deliveries), 6)
        self.assertEqual(self.state.get("ivan", 333).reminders_sent, 2)
        await process_dialog(fake_cfg(max_reminders=2), OP, client, dialog, self.state, notifier, 222,
                             BASE + timedelta(minutes=30))
        self.assertEqual(len(notifier.deliveries), 6)

    async def test_disable_repeats_and_same_main_operator_id(self):
        cfg = fake_cfg(remind_every_seconds=0)
        messages = [FakeMessage(1, 85)]
        client = FakeClient(messages)
        notifier = FakeNotifier()
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222, BASE)
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222, BASE + timedelta(days=2))
        self.assertEqual(len(notifier.deliveries), 2)
        self.state.close()
        self.state = State(str(Path(self.tmp.name) / "other.sqlite"))
        cfg["main_telegram_id"] = 222
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222, BASE)
        self.assertEqual([uid for uid, _ in notifier.deliveries].count(222), 2)  # Only one extra main-only alert.

    async def test_first_start_can_skip_old_chat(self):
        cfg = fake_cfg(alert_existing_chats_on_start=False)
        messages = [FakeMessage(1, 180)]
        client = FakeClient(messages)
        notifier = FakeNotifier()
        await process_dialog(cfg, OP, client, fake_dialog(messages), self.state, notifier, 222, BASE)
        self.assertEqual(notifier.deliveries, [])
        client.messages.append(FakeMessage(2, 65))
        await process_dialog(cfg, OP, client, fake_dialog(client.messages), self.state, notifier, 222, BASE)
        self.assertEqual(self.state.get("ivan", 333).first_unanswered_id, 2)
        self.assertEqual(len(notifier.deliveries), 2)


if __name__ == "__main__":
    unittest.main()

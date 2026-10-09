# copyright by berlonak
# telegram: @Kilax123
"""Low-request Telegram SLA polling: unanswered messages and/or genuinely unread leads."""
import asyncio
from collections import Counter
import contextvars
from datetime import datetime, timezone
import logging
import time

from .formatting import build_alerts
from .state import ChatState, UnreadState
from .timeutils import as_utc, format_local, humanize_seconds, in_quiet_hours

LOGGER = logging.getLogger(__name__)

# Telegram's own service-notification account (login codes, security notices).
SERVICE_USER_IDS = frozenset({777000})
# A chat absent from this many COMPLETE sweeps in a row is no longer tracked:
# deleted chat or account, archived with include_archived: false, excluded.
LOST_AFTER_PASSES = 3
# The developer hears about sweeps that keep failing only after this many in a row.
FAILED_PASSES_ALERT = 3
# A Telegram FloodWait at least this long is reported to the developer.
LONG_FLOOD_WAIT_SECONDS = 600
RECONNECT_MIN_DELAY = 5
RECONNECT_MAX_DELAY = 60
# No sweep progress for this long (outside known sleeps) = a hung request.
# Must exceed Telethon's own reconnect budget (~5.5 min when packets are dropped).
STALL_LIMIT_SECONDS = 600
# connect()+login may hang forever on a "black hole" network (TCP connects, no
# replies: DPI throttling, broken VPN/proxy). Bounded, then retried.
LOGIN_TIMEOUT_SECONDS = 180
WATCH_INTERVAL_SECONDS = 30
# asyncio.TimeoutError is an OSError subclass only since Python 3.11.
CONNECTION_ERRORS = (ConnectionError, OSError, asyncio.TimeoutError)
# Heartbeat of the sweep running in the current task (see _Progress).
_PROGRESS = contextvars.ContextVar("sla_sweep_progress", default=None)


def _heartbeat():
    progress = _PROGRESS.get()
    if progress is not None:
        progress.beat()


class RequestPacer:
    """Spacing between history scans of different dialogs, PER operator account.

    Pages of a single long history are spaced separately by Telethon's
    iter_messages(wait_time=...), not by this pacer.
    """
    def __init__(self, delay):
        self.delay = float(delay)
        self._next = 0.0

    async def wait(self):
        loop = asyncio.get_running_loop()
        remaining = self._next - loop.time()
        if remaining > 0:
            LOGGER.debug('Пауза защиты от FloodWait перед чтением истории: %.2f сек.', remaining)
            await asyncio.sleep(remaining)
        self._next = loop.time() + self.delay


def is_real_message(message):
    """Ignore service events, which cannot be a genuine customer request or reply."""
    return bool(getattr(message, "message", None) or getattr(message, "media", None))


def is_auto_reply(message):
    """Telegram Business greeting/away messages (flag `offline`) and replies of a
    connected business bot: outgoing, but NOT an operator's answer."""
    return bool(getattr(message, "out", False) and (
        getattr(message, "offline", False) or getattr(message, "via_business_bot_id", None)))


def counts_for_sla(message):
    return is_real_message(message) and not is_auto_reply(message)


def _ts(message):
    return int(as_utc(message.date).timestamp())


def history_cutoff(cfg, now=None):
    """Epoch seconds before which incoming messages never open a NEW case."""
    hours = cfg.get("history_max_age_hours", 0) or 0
    if hours <= 0:
        return None
    current = now if now is not None else datetime.now(timezone.utc)
    return int(current.timestamp()) - int(hours) * 3600


def staff_ids(cfg):
    cached = cfg.get("_staff_ids")
    if cached is not None:
        return cached
    ids = {cfg.get("main_telegram_id"), cfg.get("developer_telegram_id")}
    ids.update(item.get("operator_telegram_id") for item in cfg.get("sessions", None) or [])
    return frozenset(value for value in ids if isinstance(value, int))


def apply_message(s, msg):
    """Consume old-to-new Telegram messages, retaining oldest still unanswered."""
    if not counts_for_sla(msg):
        return
    if msg.out:
        s.first_unanswered_id = None
        s.first_unanswered_ts = None
        s.reset_alerts()
    elif s.first_unanswered_id is None:
        s.first_unanswered_id = msg.id
        s.first_unanswered_ts = _ts(msg)
        s.reset_alerts()


async def _paced_history(client, entity, pacer=None, wait_time=None, **kwargs):
    if pacer is not None:
        await pacer.wait()
    LOGGER.debug('GetHistory: чат=%s limit=%s min_id=%s reverse=%s wait_time=%s',
                 getattr(entity, 'id', '?'), kwargs.get('limit'), kwargs.get('min_id'),
                 kwargs.get('reverse'), wait_time)
    count = 0
    # Passing wait_time even on a one-page request is harmless; Telethon uses
    # it when iter_messages needs a subsequent GetHistory page.
    if wait_time is not None:
        kwargs["wait_time"] = wait_time
    try:
        async for msg in client.iter_messages(entity, **kwargs):
            count += 1
            _heartbeat()  # a long but moving history scan is not a hang
            yield msg
    finally:
        LOGGER.debug('GetHistory: чат=%s получено=%d сообщений',
                     getattr(entity, 'id', '?'), count)


async def _message_exists(client, entity, message_id, pacer=None, cache=None):
    """False only when Telegram confirms the message is gone (deleted by the lead).

    cache: one dict per processed dialog, so the unanswered and unread clocks
    never pay twice for the same message."""
    getter = getattr(client, "get_messages", None)
    if getter is None or message_id is None:
        return True
    if cache is not None and message_id in cache:
        return cache[message_id]
    if pacer is not None:
        await pacer.wait()
    found = await getter(entity, ids=message_id)
    _heartbeat()
    LOGGER.debug('GetMessages: чат=%s сообщение=%s существует=%s',
                 getattr(entity, 'id', '?'), message_id, found is not None)
    if cache is not None:
        cache[message_id] = found is not None
    return found is not None


async def _recheck_unanswered_case(client, entity, s, pacer, page_delay, history_limit, cache=None):
    """If the lead deleted the message the open case counts from, re-anchor the
    clock on the oldest remaining unanswered message. Returns the withdrawn id."""
    case_id = s.first_unanswered_id
    if await _message_exists(client, entity, case_id, pacer, cache):
        return None
    replacement = None
    async for msg in _paced_history(client, entity, pacer, page_delay,
                                    min_id=case_id, limit=history_limit):
        if counts_for_sla(msg):
            if msg.out:
                break
            replacement = msg  # newest -> oldest: the last kept is the oldest
    LOGGER.info("Чат %s: сообщение %s удалено из чата; обращение %s", entity.id, case_id,
                f"теперь считается от {replacement.id}" if replacement else "закрыто")
    s.first_unanswered_id = replacement.id if replacement is not None else None
    s.first_unanswered_ts = _ts(replacement) if replacement is not None else None
    s.reset_alerts()
    return case_id


async def refresh_chat(client, entity, latest, previous, history_limit, include_existing=True,
                       pacer=None, history_page_delay=None, message_hook=None, cutoff_ts=None,
                       lookup_cache=None):
    """Unanswered clock: bounded first scan; later only newly arrived messages.

    Returns (state, preview, withdrawn_case_id); withdrawn_case_id is set when the
    lead deleted the message the open case was counted from.
    """
    if previous is None:
        s = ChatState(last_seen_id=latest.id)
        if not include_existing:
            return s, (latest if is_real_message(latest) and not latest.out else None), None
        if counts_for_sla(latest) and latest.out:
            return s, None, None
        if cutoff_ts is not None and _ts(latest) < cutoff_ts:
            # The whole conversation is older than the history window:
            # not a current lead, and no GetHistory is spent on it.
            return s, None, None
        oldest_unanswered = None
        last_real = None
        seen = 0
        async for msg in _paced_history(client, entity, pacer, history_page_delay, limit=history_limit + 1):
            if seen == history_limit:
                LOGGER.warning("Чат %s: лимит истории %s достигнут; первое сообщение может быть ещё старше",
                               entity.id, history_limit)
                break
            seen += 1
            if cutoff_ts is not None and _ts(msg) < cutoff_ts:
                break  # older messages cannot open a case
            if counts_for_sla(msg):
                if last_real is None:
                    last_real = msg
                if msg.out:
                    break
                oldest_unanswered = msg
        if oldest_unanswered is not None:
            s.first_unanswered_id = oldest_unanswered.id
            s.first_unanswered_ts = _ts(oldest_unanswered)
        return s, last_real, None

    s = previous
    case_before = s.first_unanswered_id
    if latest.id <= s.last_seen_id:
        withdrawn = None
        if latest.id < s.last_seen_id:
            # Only deletion makes the newest message older than before.
            if case_before is not None:
                withdrawn = await _recheck_unanswered_case(client, entity, s, pacer,
                                                           history_page_delay, history_limit, lookup_cache)
            s.last_seen_id = latest.id  # messages above it no longer exist
        return s, (latest if is_real_message(latest) and not latest.out else None), withdrawn
    if counts_for_sla(latest) and latest.out and message_hook is None:
        apply_message(s, latest)
        s.last_seen_id = latest.id
        return s, None, None

    # Only newly arrived messages; zero GetHistory requests if the dialog has
    # not changed. A reply between two incoming messages is handled in order.
    newer = [msg async for msg in _paced_history(
        client, entity, pacer, history_page_delay, min_id=s.last_seen_id)]
    if not newer:
        return s, (latest if is_real_message(latest) and not latest.out else None), None
    s.last_seen_id = max(s.last_seen_id, max(msg.id for msg in newer))
    for msg in reversed(newer):
        old_id, old_ts = s.first_unanswered_id, s.first_unanswered_ts
        apply_message(s, msg)
        if message_hook is not None and counts_for_sla(msg):
            message_hook(msg, old_id, old_ts, s)
    withdrawn = None
    if case_before is not None and s.first_unanswered_id == case_before:
        # The lead wrote again while unanswered: the case start must still exist.
        withdrawn = await _recheck_unanswered_case(client, entity, s, pacer,
                                                   history_page_delay, history_limit, lookup_cache)
    last_real = next((msg for msg in newer if counts_for_sla(msg)), None)
    return s, (last_real if last_real is not None and not last_real.out else None), withdrawn


def _read_max_id(dialog):
    """Telethon Dialog.dialog is the raw Dialog containing read_inbox_max_id."""
    raw = getattr(dialog, "dialog", None)
    marker = getattr(raw, "read_inbox_max_id", None)
    return marker if isinstance(marker, int) and marker >= 0 else None


async def _find_first_unread(client, entity, lower_bound, scan_limit, pacer, page_delay, cutoff_ts):
    """Oldest genuine incoming message above the read position.

    cutoff_ts (only when discovering a NEW case): messages older than the
    history window are ignored, so a years-old unread chat is not a current lead.
    """
    new_first = None
    examined = 0
    if cutoff_ts is None:
        async for msg in _paced_history(client, entity, pacer, page_delay,
                                        min_id=lower_bound, reverse=True, limit=scan_limit):
            examined += 1
            if is_real_message(msg) and not msg.out:
                new_first = msg
                break
        if new_first is None and examined >= scan_limit:
            LOGGER.warning("Чат %s: unread_scan_limit=%s недостаточен для поиска входящего",
                           entity.id, scan_limit)
        return new_first, examined
    async for msg in _paced_history(client, entity, pacer, page_delay,
                                    min_id=lower_bound, limit=scan_limit):
        examined += 1
        if _ts(msg) < cutoff_ts:
            break
        if is_real_message(msg) and not msg.out:
            new_first = msg  # newest -> oldest: keep the oldest inside the window
    if examined >= scan_limit:
        LOGGER.warning("Чат %s: непрочитанных в окне больше unread_scan_limit=%s; "
                       "отсчёт от самого старого из просмотренных", entity.id, scan_limit)
    return new_first, examined


async def refresh_unread_chat(client, entity, dialog, previous, scan_limit, include_existing=True,
                              pacer=None, history_page_delay=None, cutoff_ts=None, lookup_cache=None):
    """Track *real* unread incoming messages using Telegram's read position.

    unread_mark (the manual "mark as unread" flag) is deliberately never used.
    A real read is verified by read_inbox_max_id crossing the message ID, even
    when read and manual re-mark happen entirely between two polling passes.
    Missing/regressed markers suspend alerts rather than fabricate an unread
    case from unread_count alone.
    Returns (state, preview, verified_snapshot, withdrawn_case_id).
    """
    latest = dialog.message
    unread_count = max(0, int(getattr(dialog, "unread_count", 0) or 0))
    reported_marker = _read_max_id(dialog)
    previous_known = previous.read_marker_known if previous is not None else False
    old_last_seen = previous.last_seen_id if previous is not None else 0
    old_unread_count = previous.last_unread_count if previous is not None else 0
    old_marker = previous.last_read_max_id if previous is not None else 0
    s = previous if previous is not None else UnreadState()
    last_incoming = latest if is_real_message(latest) and not latest.out else None
    withdrawn = None

    LOGGER.debug('Непрочитанные: чат=%s last_id=%s unread_count=%s unread_mark=%s '
                 'read_inbox_max_id=%s прошлый_первый=%s прошлый_read_max=%s '
                 'прошлый_счётчик=%s', entity.id, latest.id, unread_count,
                 getattr(getattr(dialog, 'dialog', None), 'unread_mark', None),
                 reported_marker, s.first_unread_id, old_marker, old_unread_count)

    if previous is None and not include_existing:
        # Set the baseline even if the read marker is temporarily unavailable.
        # The very first pass is still the beginning of monitoring.
        s.ignored_through_id = latest.id

    # Raw Telethon Dialog.dialog includes read_inbox_max_id (including zero).
    # An absent/stale marker cannot prove that a formerly unread message was
    # not read between polling cycles. Preserve the case but neither notify
    # nor mark it resolved until a fresh trusted snapshot is available.
    if reported_marker is None:
        if previous is None or previous_known:
            LOGGER.warning("Чат %s: нет read_inbox_max_id; уведомления о непрочитанном "
                           "приостановлены до следующего корректного снимка", entity.id)
        s.last_seen_id = latest.id
        return s, last_incoming, False, None
    if (previous_known or old_marker > 0) and reported_marker < old_marker:
        LOGGER.warning("Чат %s: позиция прочтения уменьшилась с %s до %s; "
                       "пропускаю непоследовательный снимок", entity.id,
                       old_marker, reported_marker)
        # Do not overwrite the highest previously confirmed position.
        s.last_seen_id = latest.id
        return s, last_incoming, False, None

    s.read_marker_known = True
    s.last_read_max_id = max(old_marker if previous_known else 0, reported_marker)
    # The newest ID as seen now (it goes DOWN when the lead deletes messages).
    s.last_seen_id = latest.id

    if previous is None and not include_existing:
        s.last_unread_count = unread_count
        return s, last_incoming, True, None

    # Zero unread messages alone is not sufficient to label a tracked message
    # READ if the server's confirmed position has not reached that message.
    # Either Telegram's snapshot is inconsistent, or the lead deleted it.
    if unread_count == 0:
        if s.first_unread_id is not None and reported_marker < s.first_unread_id:
            if await _message_exists(client, entity, s.first_unread_id, pacer, lookup_cache):
                LOGGER.warning("Чат %s: счётчик непрочитанных равен 0, но позиция "
                               "прочтения ещё не дошла до сообщения %s; жду "
                               "подтверждения", entity.id, s.first_unread_id)
                return s, last_incoming, False, None
            withdrawn = s.first_unread_id
            LOGGER.info("Чат %s: непрочитанное сообщение %s удалено из чата; обращение закрыто",
                        entity.id, withdrawn)
        if s.first_unread_id is not None or s.pending_main or s.pending_operator:
            s.first_unread_id = None
            s.first_unread_ts = None
            s.reset_alerts()
        s.ignored_through_id = 0
        s.last_unread_count = 0
        return s, None, True, withdrawn

    first_was_read = (s.first_unread_id is not None and
                      s.first_unread_id <= reported_marker)
    first_is_ignored = (s.first_unread_id is not None and
                        s.first_unread_id <= s.ignored_through_id)
    # A NEW case is being discovered (not a continuation after a partial read):
    # only then does the history window apply.
    discovery = s.first_unread_id is None or first_is_ignored
    # Count changes alone are insufficient evidence that a specific message
    # was read; unlike the old fallback this never guesses from last N posts.
    need_scan = s.first_unread_id is None or first_was_read or first_is_ignored
    if (previous is not None and previous_known and s.first_unread_id is None and
            latest.id <= old_last_seen and unread_count == old_unread_count and
            reported_marker == old_marker):
        # Same no-match snapshot already scanned: don't burn GetHistory calls.
        need_scan = False
    if s.first_unread_id is None and s.ignored_through_id and latest.id <= s.ignored_through_id:
        need_scan = False
    if need_scan and discovery and cutoff_ts is not None and _ts(latest) < cutoff_ts:
        need_scan = False  # everything in this chat is older than the history window

    # The lead may delete the very message the open case counts from, possibly
    # writing again. Checked only when the chat changed, at one request.
    if (not need_scan and s.first_unread_id is not None and previous is not None and
            (latest.id != old_last_seen or unread_count != old_unread_count)):
        if not await _message_exists(client, entity, s.first_unread_id, pacer, lookup_cache):
            withdrawn = s.first_unread_id
            LOGGER.info("Чат %s: непрочитанное сообщение %s удалено из чата; ищу следующее",
                        entity.id, withdrawn)
            s.first_unread_id = None
            s.first_unread_ts = None
            s.reset_alerts()
            need_scan = True  # continuation from the read position, no window

    LOGGER.debug('Непрочитанные: чат=%s требуется_поиск=%s read_marker=%s '
                 'ignored_through=%s previous_first=%s',
                 entity.id, need_scan, reported_marker, s.ignored_through_id, s.first_unread_id)
    if need_scan:
        lower_bound = max(s.ignored_through_id, reported_marker)
        # Only messages ABOVE the server's read position can be considered.
        # A manually marked old dialog has no such messages, even if its UI
        # says "unread". The very first genuine incoming starts the clock.
        new_first, examined = await _find_first_unread(
            client, entity, lower_bound, scan_limit, pacer, history_page_delay,
            cutoff_ts if discovery else None)
        new_id = new_first.id if new_first is not None else None
        if new_id != s.first_unread_id:
            s.first_unread_id = new_id
            s.first_unread_ts = _ts(new_first) if new_first is not None else None
            s.reset_alerts()
        if last_incoming is None:
            last_incoming = new_first
        LOGGER.debug('Непрочитанные: чат=%s просканировано=%s новый_первый=%s',
                     entity.id, examined, new_id)

    s.last_unread_count = unread_count
    LOGGER.debug('Непрочитанные: чат=%s итог first=%s first_ts=%s read_max=%s '
                 'повторов=%s доставлено_в=%s', entity.id, s.first_unread_id,
                 s.first_unread_ts, s.last_read_max_id, s.reminders_sent, s.last_completed_ts)
    return s, last_incoming, True, withdrawn


async def _deliver(notifier, recipient, message, want_id):
    """(status, bot_message_id, description); status: ok | permanent | transient."""
    deliver = getattr(notifier, "deliver", None)
    if deliver is not None:
        result = await deliver(recipient, message)
        return result.status, result.message_id, result.description
    if want_id and hasattr(notifier, "send_tracked"):
        delivered, bot_message_id = await notifier.send_tracked(recipient, message)
    else:
        delivered, bot_message_id = await notifier.send(recipient, message), None
    return ("ok" if delivered else "transient"), bot_message_id, None


async def deliver_due(cfg, sess_cfg, entity, chat_id, dialog, clock_ts, chat, notifier,
                      save, last_incoming, kind, now=None, track_message=None, tech=None):
    """Share consistent retry/quiet-hour/reminder semantics across two independent clocks.

    Each recipient is independent: a permanently undeliverable one (bot blocked,
    /start never pressed) is skipped for this cycle and reported to the
    developer, so the other recipient keeps getting reminders.
    """
    if clock_ts is None:
        LOGGER.debug('Уведомление %s: чат=%s активной просрочки нет', kind, chat_id)
        return
    fixed_now = now is not None
    now = now if fixed_now else datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    age = now_ts - clock_ts
    if age < cfg["sla_seconds"]:
        LOGGER.debug('Уведомление %s: чат=%s ожидание=%ss, SLA=%ss, ещё рано',
                     kind, chat_id, age, cfg['sla_seconds'])
        return
    if in_quiet_hours(now, cfg["quiet_window"], cfg["utc_offset_hours"]):
        LOGGER.debug('Уведомление %s: чат=%s ожидание=%ss, тихие часы — пропуск',
                     kind, chat_id, age)
        return

    operator_id = sess_cfg.get("operator_telegram_id")
    send_operator = bool(cfg["notify_operator"] and operator_id != cfg["main_telegram_id"])
    if not (chat.pending_main or chat.pending_operator):
        if chat.last_completed_ts is None:
            due = True
        else:
            interval = cfg["remind_every_seconds"]
            remaining = cfg["max_reminders"] == 0 or chat.reminders_sent < cfg["max_reminders"]
            due = bool(interval and remaining and now_ts - chat.last_completed_ts >= interval)
        if not due:
            LOGGER.debug('Уведомление %s: чат=%s ожидание=%ss, последнее=%s, '
                         'интервал_повтора=%ss, повторов=%s — пока не пора',
                         kind, chat_id, age, chat.last_completed_ts,
                         cfg['remind_every_seconds'], chat.reminders_sent)
            return
        LOGGER.debug('Уведомление %s: чат=%s пора отправлять; pending_main=%s '
                     'pending_operator=%s', kind, chat_id, chat.pending_main, chat.pending_operator)
        chat.pending_main = True
        chat.pending_operator = send_operator
        save(chat)

    # Preview is optional, notably if the newest message is outgoing in unread
    # mode. Do NOT fetch a whole history only to format a notification.
    main_text, operator_text = build_alerts(
        entity, sess_cfg["operator_label"], clock_ts,
        last_incoming, getattr(dialog, "unread_count", 0), cfg["utc_offset_hours"],
        cfg["message_preview_len"], now, kind=kind,
    )
    not_quiet = lambda: not in_quiet_hours(
        now if fixed_now else datetime.now(timezone.utc), cfg["quiet_window"], cfg["utc_offset_hours"])

    async def send_and_track(recipient, message, who):
        LOGGER.debug('Bot API: отправка %s чата=%s получатель=%s', kind, chat_id, recipient)
        status, bot_message_id, description = await _deliver(
            notifier, recipient, message, track_message is not None)
        LOGGER.debug('Bot API: результат %s чата=%s получатель=%s статус=%s '
                     'bot_message_id=%s', kind, chat_id, recipient, status, bot_message_id)
        if status == "ok":
            if bot_message_id is not None and track_message is not None:
                track_message(kind, recipient, bot_message_id, message)
            if tech is not None:
                await tech.resolved(f"recipient:{recipient}",
                                    f"🟢 Бот снова доставляет уведомления {who} (Telegram ID {recipient}).")
        elif status == "permanent" and tech is not None:
            from .devalerts import esc
            await tech.problem(
                f"recipient:{recipient}",
                f"🔴 Бот не может доставить уведомление {who} (Telegram ID {recipient}): "
                f"<i>{esc(description or 'отказ Telegram')}</i>.\n"
                "Этому получателю нужно открыть бота и нажать /start (или разблокировать бота). "
                "Пока это не исправлено, он уведомлений не получает; остальным они идут.")
        return status

    from .devalerts import esc
    operator_who = f"оператору «{esc(sess_cfg['operator_label'])}»"
    # A permanent refusal is not retried every pass: this cycle is over for
    # that recipient (tried again at the next reminder). Transient: retry.
    if chat.pending_main and not_quiet():
        if await send_and_track(cfg["main_telegram_id"], main_text, "руководителю") != "transient":
            chat.pending_main = False
            save(chat)
    if chat.pending_operator and not_quiet():
        if await send_and_track(operator_id, operator_text, operator_who) != "transient":
            chat.pending_operator = False
            save(chat)

    if not chat.pending_main and not chat.pending_operator:
        if chat.last_completed_ts is not None:
            chat.reminders_sent += 1
        chat.last_completed_ts = int((now if fixed_now else datetime.now(timezone.utc)).timestamp())
        save(chat)
        LOGGER.info("[%s] %s уведомления обработаны по чату %s; повторов: %s",
                    sess_cfg["name"], "Непрочитано:" if kind == "unread" else "Без ответа:",
                    chat_id, chat.reminders_sent)


def dialog_skip_reason(dialog, cfg, me_id):
    """Use the *same* private-lead filter for scan counts and actual processing.

    `iter_dialogs()` returns channels, groups and bots too.  Never call
    GetHistory for them, and never report them as checked customer chats.
    Telegram's UI folders (e.g. Personal) are separate filters and are not
    implicitly applied to the account-wide dialog list.
    """
    if getattr(dialog, "is_group", False):
        return "группы"
    if getattr(dialog, "is_channel", False):
        return "каналы"
    if not getattr(dialog, "is_user", False):
        return "прочие_не_личные"
    entity = getattr(dialog, "entity", None)
    cid = getattr(entity, "id", None)
    if not isinstance(cid, int) or cid <= 0:
        return "прочие_не_личные"
    # Telethon's raw Dialog.peer is PeerUser for real one-to-one chats.
    # This second check prevents accidental misclassification if flags
    # are inconsistent. Test doubles without the raw peer are supported.
    raw_peer = getattr(getattr(dialog, "dialog", None), "peer", None)
    if raw_peer is not None and (type(raw_peer).__name__ != "PeerUser" or
                                  getattr(raw_peer, "user_id", None) != cid):
        return "прочие_не_личные"
    if cid == me_id:
        return "свой_аккаунт"
    if cid in SERVICE_USER_IDS or getattr(entity, "support", False) is True:
        return "служебные_telegram"
    if cid in staff_ids(cfg):
        return "сотрудники"
    if getattr(entity, "bot", False):
        return "боты"
    if getattr(entity, "deleted", False):
        return "удалённые"
    if getattr(dialog, "message", None) is None:
        return "без_сообщений"
    if cfg.get("include_ids") and cid not in cfg["include_ids"]:
        return "не_в_include_ids"
    if cid in cfg.get("exclude_ids", []):
        return "в_exclude_ids"
    if cfg.get("only_contacts", False) and not getattr(entity, "contact", False):
        return "не_контакт"
    return None


async def process_dialog(cfg, sess_cfg, client, dialog, state, notifier, me_id, now=None, pacer=None,
                         tech=None):
    """Only actual private leads; same guard as in the main sweep."""
    reason = dialog_skip_reason(dialog, cfg, me_id)
    if reason is not None:
        LOGGER.debug('Диалог %s пропущен: %s', getattr(dialog, 'id', '?'), reason)
        return
    entity = dialog.entity
    cid = entity.id
    LOGGER.debug('Личный чат: чат=%s latest=%s me=%s unread_count=%s', cid,
                 dialog.message.id, me_id, getattr(dialog, 'unread_count', None))

    session_name = sess_cfg["name"]
    mode = cfg.get("alert_mode", "unanswered")
    mark_enabled = cfg.get("edit_resolved_alerts", True)
    stats_enabled = cfg.get("stats_enabled", True)
    page_delay = cfg.get("history_page_delay_seconds")
    cutoff = history_cutoff(cfg, now)
    lookup_cache = {}

    def moment():
        return int((now or datetime.now(timezone.utc)).timestamp())

    def track(kind, case_id):
        if not mark_enabled:
            return None
        def remember(_kind, recipient, msg_id, body):
            state.record_alert(session_name, cid, kind, case_id, recipient, msg_id, body)
            # Read update can arrive while Bot API is sending this message.
            # Ensure even a late-recorded message gets the green mark.
            if kind == 'unread':
                current = state.get_unread(session_name, cid)
                if (current is not None and current.read_marker_known and
                        current.last_read_max_id >= case_id and
                        current.first_unread_id != case_id):
                    LOGGER.debug('Бот ответил после подтверждения прочтения: '
                                 'поставить отложенную галку чат=%s case=%s', cid, case_id)
                    state.mark_resolved(session_name, cid, kind, case_id,
                                        int(datetime.now(timezone.utc).timestamp()))
        return remember

    from .statistics import open_case, close_case, withdraw_case
    # Collect answer statistics in every mode, even when alerts are unread-only.
    # Uses the same bounded, change-only history scan and the same FloodWait pacer.
    if mode in ("unanswered", "both") or stats_enabled:
        previous = state.get(session_name, cid)
        if stats_enabled and previous is not None:
            open_case(state.conn, "answers", session_name, cid,
                      previous.first_unanswered_id, previous.first_unanswered_ts)

        def on_message(msg, old_id, old_ts, new_state):
            msg_moment = _ts(msg)
            if msg.out and old_id is not None:
                if mark_enabled and mode in ("unanswered", "both"):
                    state.mark_resolved(session_name, cid, "unanswered", old_id, msg_moment)
                if stats_enabled:
                    close_case(state.conn, "answers", session_name, cid, old_id, msg_moment)
            if (stats_enabled and not msg.out and old_id is None
                    and new_state.first_unanswered_id is not None):
                open_case(state.conn, "answers", session_name, cid,
                          new_state.first_unanswered_id, new_state.first_unanswered_ts)
        previous_values = vars(previous).copy() if previous is not None else None
        chat, preview, withdrawn = await refresh_chat(
            client, entity, dialog.message, previous, cfg["history_scan_limit"],
            cfg["alert_existing_chats_on_start"], pacer, page_delay,
            on_message if (stats_enabled or mark_enabled) else None,
            cutoff_ts=cutoff, lookup_cache=lookup_cache,
        )
        if withdrawn is not None:
            if mark_enabled and mode in ("unanswered", "both"):
                state.mark_resolved(session_name, cid, "unanswered", withdrawn, moment(), reason="deleted")
            if stats_enabled:
                withdraw_case(state.conn, "answers", session_name, cid, withdrawn, moment(), cfg["sla_seconds"])
        if stats_enabled:
            open_case(state.conn, "answers", session_name, cid,
                      chat.first_unanswered_id, chat.first_unanswered_ts)
        if previous_values is None or vars(chat) != previous_values:
            state.save(session_name, cid, chat)
        if mode in ("unanswered", "both") and chat.first_unanswered_ts is not None:
            # Only for a DUE alert with an outgoing/service latest: recover
            # preview, but do not pay for it on every healthy or recent chat.
            current_now = now if now is not None else datetime.now(timezone.utc)
            is_due = int(current_now.timestamp()) - chat.first_unanswered_ts >= cfg["sla_seconds"]
            is_repeat_due = chat.last_completed_ts is None or (cfg["remind_every_seconds"] and
                             int(current_now.timestamp()) - chat.last_completed_ts >= cfg["remind_every_seconds"])
            if (preview is None and is_due and (is_repeat_due or chat.pending_main or chat.pending_operator)
                    and not in_quiet_hours(current_now, cfg["quiet_window"], cfg["utc_offset_hours"])):
                # No extra history lookup is necessary for the SLA itself.
                # Not knowing the preview is better than a flood wait.
                preview = dialog.message if is_real_message(dialog.message) and not dialog.message.out else None
            await deliver_due(cfg, sess_cfg, entity, cid, dialog, chat.first_unanswered_ts,
                              chat, notifier, lambda s: state.save(session_name, cid, s),
                              preview, "unanswered", now, track("unanswered", chat.first_unanswered_id),
                              tech=tech)

    if mode in ("unread", "both") or stats_enabled:
        previous = state.get_unread(session_name, cid)
        if stats_enabled and previous is not None:
            open_case(state.conn, "reads", session_name, cid,
                      previous.first_unread_id, previous.first_unread_ts)
        previous_values = vars(previous).copy() if previous is not None else None
        chat, preview, read_snapshot_ok, withdrawn = await refresh_unread_chat(
            client, entity, dialog, previous, cfg.get("unread_scan_limit", 200),
            cfg["alert_existing_chats_on_start"], pacer, page_delay, cutoff_ts=cutoff,
            lookup_cache=lookup_cache,
        )
        # A raw read update may have arrived during GetHistory. Never replace
        # its fresher server marker with a stale dialog snapshot or notify for
        # a case already resolved by that update.
        freshest = state.get_unread(session_name, cid)
        if (freshest is not None and previous_values is not None and
                (freshest.last_read_max_id > chat.last_read_max_id or
                 (freshest.first_unread_id != chat.first_unread_id and
                  freshest.last_read_max_id >= (chat.first_unread_id or 0) and
                  freshest.last_read_max_id > previous_values['last_read_max_id']))):
            LOGGER.debug('Чат=%s: во время опроса пришло более свежее событие прочтения '
                         '(read_max=%s), устаревший снимок игнорируется',
                         cid, freshest.last_read_max_id)
            return
        if withdrawn is not None:
            if mark_enabled and mode in ("unread", "both"):
                state.mark_resolved(session_name, cid, "unread", withdrawn, moment(), reason="deleted")
            if stats_enabled:
                withdraw_case(state.conn, "reads", session_name, cid, withdrawn, moment(), cfg["sla_seconds"])
        before = previous_values["first_unread_id"] if previous_values else None
        LOGGER.debug('Чат=%s: unread old_case=%s new_case=%s snapshot_ok=%s',
                     cid, before, chat.first_unread_id, read_snapshot_ok)
        if before != chat.first_unread_id:
            discovered = moment()
            # Only the server read position is proof of reading; a changed
            # counter or manual unread mark cannot resolve a message.
            marker = _read_max_id(dialog)
            read_confirmed = (read_snapshot_ok and before is not None and before != withdrawn and
                              marker is not None and marker >= before)
            LOGGER.debug('Чат=%s: смена unread case=%s -> %s; read_confirmed=%s '
                         'read_marker=%s', cid, before, chat.first_unread_id,
                         read_confirmed, marker)
            if read_confirmed and mark_enabled and mode in ("unread", "both"):
                state.mark_resolved(session_name, cid, "unread", before, discovered)
            if stats_enabled:
                if read_confirmed:
                    close_case(state.conn, "reads", session_name, cid, before, discovered)
                open_case(state.conn, "reads", session_name, cid,
                          chat.first_unread_id, chat.first_unread_ts)
        if previous_values is None or vars(chat) != previous_values:
            state.save_unread(session_name, cid, chat)
        if read_snapshot_ok and mode in ("unread", "both"):
            await deliver_due(cfg, sess_cfg, entity, cid, dialog, chat.first_unread_ts,
                              chat, notifier, lambda s: state.save_unread(session_name, cid, s),
                              preview, "unread", now, track("unread", chat.first_unread_id),
                              tech=tech)


def apply_read_update(cfg, sess_cfg, state, chat_id, max_read_id, now_ts=None):
    """React to a trusted Telegram UpdateReadHistoryInbox without GetHistory.

    Telegram can deliver a read update while a large dialog pass is running.
    This confirms the read server-side and queues edits for ALL sent alerts.
    Ordinary polling remains the fallback for updates missed while offline.
    """
    if not isinstance(max_read_id, int) or max_read_id < 0:
        return False
    session = sess_cfg['name']
    chat = state.get_unread(session, chat_id)
    if chat is None:
        LOGGER.debug('[%s] ReadUpdate чат=%s max_id=%s: нет сохранённого состояния',
                     session, chat_id, max_read_id)
        return False
    if chat.read_marker_known and max_read_id < chat.last_read_max_id:
        LOGGER.debug('[%s] ReadUpdate чат=%s max_id=%s: более старый маркер, игнорирую',
                     session, chat_id, max_read_id)
        return False
    old_id = chat.first_unread_id
    chat.last_read_max_id = max(chat.last_read_max_id, max_read_id)
    chat.read_marker_known = True
    resolved = old_id is not None and max_read_id >= old_id
    if resolved:
        chat.first_unread_id = None
        chat.first_unread_ts = None
        chat.reset_alerts()
        if cfg.get('edit_resolved_alerts', True) and cfg.get('alert_mode') in ('unread', 'both'):
            now_ts = now_ts if now_ts is not None else int(datetime.now(timezone.utc).timestamp())
            state.mark_resolved(session, chat_id, 'unread', old_id, now_ts)
        if cfg.get('stats_enabled', True):
            from .statistics import close_case
            close_case(state.conn, 'reads', session, chat_id, old_id,
                       now_ts if now_ts is not None else int(datetime.now(timezone.utc).timestamp()))
        LOGGER.info('[%s] Чат=%s: Telegram подтвердил прочтение сообщения %s; '
                    'галочки поставлены в очередь', session, chat_id, old_id)
    else:
        LOGGER.debug('[%s] ReadUpdate чат=%s max_id=%s active=%s — '
                     'позиция прочтения сохранена', session, chat_id, max_read_id, old_id)
    state.save_unread(session, chat_id, chat)
    return resolved


def _error_types(errors, *names):
    return tuple(t for t in (getattr(errors, n, None) for n in names)
                 if isinstance(t, type) and issubclass(t, BaseException))


class _NeverRaised(Exception):
    """Placeholder when a Telethon error class is unavailable."""


async def _safe_disconnect(client):
    try:
        result = client.disconnect()
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            await result
    except asyncio.CancelledError:
        raise
    except Exception as error:
        LOGGER.debug('Ошибка при отключении клиента: %s', type(error).__name__)


async def _login(client):
    """connect() + authorization check. Returns the account or None if unauthorized.

    No sleeping in here (it runs under LOGIN_TIMEOUT_SECONDS): a FloodWait
    propagates to monitor_session, which waits exactly as long as Telegram asks.
    """
    await client.connect()
    if await client.is_user_authorized():
        return await client.get_me()
    # Telethon answers False for ANY RPC error in that check (a FloodWait
    # too); get_me() returns None only for a truly unauthorized session.
    confirm = getattr(client, "get_me", None)
    return await confirm() if confirm is not None else None


async def _check_archive_setting(client, cfg, name, who, tech):
    """New non-contact chats may be auto-archived: with include_archived: false
    those leads would silently never be monitored."""
    if cfg.get("include_archived", False):
        return
    try:
        from telethon.tl.functions.account import GetGlobalPrivacySettingsRequest
        settings = await client(GetGlobalPrivacySettingsRequest())
    except asyncio.CancelledError:
        raise
    except Exception as error:
        LOGGER.debug('[%s] не удалось проверить автоархивацию: %s', name, type(error).__name__)
        return
    if getattr(settings, "archive_and_mute_new_noncontact_peers", False) is True:
        LOGGER.warning("[%s] включена автоархивация новых чатов от не-контактов, а include_archived: false — "
                       "новые лиды в архиве не отслеживаются", name)
        await tech.problem(
            f"archive:{name}",
            f"🟡 {who}: в Telegram включено «Архивировать и отключать звук новых чатов от "
            "не-контактов», а в конфиге include_archived: false. Новые лиды попадают в архив "
            "и НЕ отслеживаются. Поставь include_archived: true или выключи эту настройку "
            "(Настройки → Конфиденциальность).", repeat=False)


def _subscribe_read_updates(client, cfg, sess_cfg, state, name):
    # Read confirmations need not wait for a 200+ dialog sweep. When online,
    # Telethon often delivers UpdateReadHistoryInbox as a push event.
    # Normal dialog polling still handles reads that happened while offline.
    if cfg.get('alert_mode') not in ('unread', 'both'):
        return
    from telethon import events, types

    async def on_read_update(update):
        if not isinstance(update, types.UpdateReadHistoryInbox):
            LOGGER.debug('[%s] RawUpdate получен: %s', name, type(update).__name__)
            return
        peer = getattr(update, 'peer', None)
        if not isinstance(peer, types.PeerUser):
            LOGGER.debug('[%s] ReadUpdate не из личной переписки: %s', name, type(peer).__name__)
            return
        LOGGER.debug('[%s] ReadUpdate: peer=%s max_id=%s still_unread=%s', name,
                     peer.user_id, update.max_id, getattr(update, 'still_unread_count', None))
        try:
            apply_read_update(cfg, sess_cfg, state, peer.user_id, update.max_id)
        except Exception:
            LOGGER.exception('[%s] Ошибка обработки ReadUpdate чат=%s', name, peer.user_id)
    client.add_event_handler(on_read_update, events.Raw())
    LOGGER.debug('[%s] Подписка на события Telegram UpdateReadHistoryInbox включена', name)


def _reconcile(state, session, seen, missed, sla_seconds=None):
    """After a COMPLETE sweep: forget chats that keep missing from it and drop
    statistics cases that no longer match the chat's current case."""
    open_chats = state.open_case_chats(session)
    for cid in list(missed):
        if cid in seen or cid not in open_chats:
            del missed[cid]
    for cid in open_chats - seen:
        missed[cid] += 1
        if missed[cid] >= LOST_AFTER_PASSES:
            del missed[cid]
            state.forget_chat(session, cid, int(time.time()), sla_seconds)
            LOGGER.info("[%s] Чат %s не встречается в %s полных обходах подряд (удалён, в архиве, "
                        "исключён или аккаунт удалён) — обращение по нему больше не отслеживается",
                        session, cid, LOST_AFTER_PASSES)
    dropped = state.drop_orphan_stats(session)
    if dropped:
        LOGGER.info("[%s] Статистика: удалено устаревших открытых обращений: %s", session, dropped)


class _Progress:
    """Heartbeat of the sweep task, watched by monitor_session."""
    def __init__(self):
        self.beat()

    def beat(self, idle_seconds=0):
        # During a known sleep "last activity" is the planned wake-up time, so an
        # outage noticed later is never backdated to before the sleep.
        self.last_wall = time.time() + idle_seconds
        self.deadline = time.monotonic() + idle_seconds + STALL_LIMIT_SECONDS

    def outage_start(self):
        return min(time.time(), self.last_wall)


class _Stalled(TimeoutError):
    pass


async def _poll_forever(cfg, sess_cfg, client, state, notifier, me_id, pacer, tech,
                        fatal_errors, flood_error, progress=None):
    """Full sweeps forever. Connection loss and a rejected session escape to the caller."""
    from .devalerts import error_text, esc
    name = sess_cfg["name"]
    who = f"Оператор «{esc(sess_cfg.get('operator_label', name))}»"
    progress = progress or _Progress()
    _PROGRESS.set(progress)  # this task's context only
    await _check_archive_setting(client, cfg, name, who, tech)
    progress.beat()
    pass_number = 0
    failed_passes = 0
    missed = Counter()
    while True:
        pass_number += 1
        pass_start = time.monotonic()
        received = 0
        eligible = 0
        checked = 0
        errors_count = 0
        skipped = Counter()
        seen = set()
        flood_wait = None
        pass_error = None
        complete = False
        progress.beat()
        LOGGER.info('[%s] Начало обхода №%s', name, pass_number)
        try:
            async for dialog in client.iter_dialogs(archived=None if cfg["include_archived"] else False):
                received += 1
                reason = dialog_skip_reason(dialog, cfg, me_id)
                if reason is not None:
                    skipped[reason] += 1
                    LOGGER.debug('[%s] Обход №%s: ID=%s отсеян (%s)', name,
                                 pass_number, getattr(dialog, 'id', '?'), reason)
                else:
                    eligible += 1
                    seen.add(dialog.entity.id)
                    chat_start = time.monotonic()
                    LOGGER.debug('[%s] Обход №%s: личный чат=%s, №%s', name,
                                 pass_number, getattr(dialog, 'id', '?'), eligible)
                    try:
                        await process_dialog(cfg, sess_cfg, client, dialog, state,
                                             notifier, me_id, pacer=pacer, tech=tech)
                        checked += 1
                    except asyncio.CancelledError:
                        raise
                    except flood_error as error:
                        flood_wait = error.seconds
                        break  # Never continue hammering other dialogs during FloodWait.
                    except fatal_errors:
                        raise
                    except CONNECTION_ERRORS:
                        raise
                    except Exception as error:
                        errors_count += 1
                        pass_error = pass_error or error
                        LOGGER.exception("[%s] ошибка проверки личного чата %s; остальные будут проверены",
                                         name, getattr(dialog, "id", "unknown"))
                    finally:
                        progress.beat()
                        LOGGER.debug('[%s] Обход №%s: личный чат=%s обработка %.2f сек.',
                                     name, pass_number, getattr(dialog, 'id', '?'),
                                     time.monotonic() - chat_start)
                if received % 30 == 0:
                    LOGGER.info('[%s] Обход №%s: получено %s диалогов, личных %s, '
                                'проверено %s, отсеяно %s; %.1f сек.', name,
                                pass_number, received, eligible, checked,
                                sum(skipped.values()), time.monotonic() - pass_start)
            else:
                complete = True
        except asyncio.CancelledError:
            raise
        except flood_error as error:
            flood_wait = error.seconds
        except fatal_errors:
            raise
        except CONNECTION_ERRORS:
            raise
        except Exception as error:
            pass_error = error
            LOGGER.exception("[%s] ошибка получения списка диалогов", name)
        breakdown = ', '.join('%s=%s' % item for item in skipped.most_common()) or 'нет'
        LOGGER.info('[%s] Обход №%s: итого получено %s диалогов; '
                    'личных подходящих %s, проверено %s, ошибок %s, '
                    'отсеяно %s (%s); %.1f сек.; FloodWait=%s',
                    name, pass_number, received, eligible, checked, errors_count,
                    sum(skipped.values()), breakdown,
                    time.monotonic() - pass_start, flood_wait)
        if flood_wait is not None:
            delay = max(1, int(flood_wait) + cfg.get("flood_wait_extra_seconds", 3))
            LOGGER.warning("[%s] Telegram запросил FloodWait %s сек.; останавливаю опрос "
                           "этой сессии на %s сек., затем повторю проход", name, flood_wait, delay)
            if delay >= LONG_FLOOD_WAIT_SECONDS:
                until = datetime.fromtimestamp(time.time() + delay, timezone.utc)
                await tech.problem(f"flood:{name}",
                                   f"🟠 {who}: Telegram ограничил запросы (FloodWait "
                                   f"{humanize_seconds(delay)}). Мониторинг этого оператора "
                                   f"приостановлен до {format_local(until, cfg.get('utc_offset_hours', 3))}.",
                                   repeat=False)
            progress.beat(idle_seconds=delay)
            await asyncio.sleep(delay)
            continue
        if complete:
            await tech.resolved(f"flood:{name}", f"🟢 {who}: ограничение Telegram закончилось, обходы идут.")
            _reconcile(state, name, seen, missed, cfg.get("sla_seconds"))
        if pass_error is not None:
            failed_passes += 1
            if failed_passes >= FAILED_PASSES_ALERT:
                await tech.problem(f"passes:{name}",
                                   f"🟠 {who}: {failed_passes} обходов подряд с ошибками "
                                   f"(последняя: {error_text(pass_error)}). Подробности — в журнале.")
        else:
            failed_passes = 0
            await tech.resolved(f"passes:{name}", f"🟢 {who}: обходы снова проходят без ошибок.")
        LOGGER.debug('[%s] Пауза после полного обхода: %s сек.',
                     name, cfg['poll_interval_seconds'])
        progress.beat(idle_seconds=cfg["poll_interval_seconds"])
        await asyncio.sleep(cfg["poll_interval_seconds"])


def _cancelled_by_telethon(client):
    """A CancelledError that is NOT a cancellation of our task: Telethon cancels
    pending request futures when it disconnects itself (its update loop hit an
    auth/SQLite/internal error). That is a lost connection, not a shutdown."""
    task = asyncio.current_task()
    cancelling = getattr(task, "cancelling", None)
    if cancelling is None or cancelling():
        return False
    is_connected = getattr(client, "is_connected", None)
    return callable(is_connected) and not is_connected()


async def _watch_poll(poll, client, progress):
    """Await the sweep task. Telethon may give up reconnecting while one of our
    requests sits in its send queue: that request never resolves. Such a hang is
    turned into ConnectionError (client.disconnected) or _Stalled (no progress)."""
    waiters = {poll}
    disconnected = getattr(client, "disconnected", None)
    if not asyncio.isfuture(disconnected):
        disconnected = None
    else:
        waiters.add(disconnected)
    try:
        while True:
            done, _ = await asyncio.wait(waiters, timeout=WATCH_INTERVAL_SECONDS,
                                         return_when=asyncio.FIRST_COMPLETED)
            if poll in done:
                return poll.result()  # re-raises the sweep's exception (incl. CancelledError)
            if disconnected is not None and disconnected in done:
                raise ConnectionError("Telethon отключился от Telegram")
            if time.monotonic() > progress.deadline:
                raise _Stalled(f"обход не продвигается {STALL_LIMIT_SECONDS} сек.")
    finally:
        if not poll.done():
            poll.cancel()
            await asyncio.gather(poll, return_exceptions=True)
        if disconnected is not None:
            if not disconnected.done():
                disconnected.cancel()
            elif not disconnected.cancelled():
                disconnected.exception()  # consume: no "exception never retrieved"


async def monitor_session(cfg, sess_cfg, state, notifier, tech=None):
    """Monitor one operator account forever.

    Lost connection (outage, sleep, Telegram restart): a fresh client reconnects
    with backoff indefinitely; the developer is told once the outage lasts
    tech_alert_delay_seconds and again when it is over. A rejected or missing
    session stops only this operator and is reported to the developer.
    """
    from telethon import TelegramClient, errors
    from .config import session_database_path
    from .devalerts import TechAlerts, error_text, esc

    tech = tech if tech is not None else TechAlerts(cfg)
    name = sess_cfg["name"]
    who = f"Оператор «{esc(sess_cfg.get('operator_label', name))}»"
    relogin = ("Нужно заново выполнить login_telethon.py для этой сессии "
               "и перезапустить монитор.")
    db_path = session_database_path(sess_cfg["session_file"])
    if not db_path.is_file():
        LOGGER.error("[%s] нет файла %s. Сначала выполните вашу логинилку с таким же "
                     "login_session_name", name, db_path)
        await tech.problem(f"session:{name}",
                           f"🔴 {who}: нет файла сессии <code>{esc(db_path.name)}</code> — "
                           f"мониторинг этого оператора не работает. {relogin}", repeat=False)
        return
    # Not the whole 406 family (AuthKeyError): only a key Telegram really killed.
    fatal_errors = _error_types(errors, "UnauthorizedError", "AuthKeyDuplicatedError", "AuthKeyNotFound")
    flood_error = (_error_types(errors, "FloodWaitError") or (_NeverRaised,))[0]
    pacer = RequestPacer(cfg.get("history_request_delay_seconds", 3.2))
    grace = cfg.get("tech_alert_delay_seconds", 300)
    down_since = None
    backoff = RECONNECT_MIN_DELAY

    async def link_restored(since):
        outage = humanize_seconds(time.time() - since)
        LOGGER.info("[%s] связь с Telegram восстановлена после простоя %s", name, outage)
        await tech.resolved(f"link:{name}", f"🟢 {who}: связь с Telegram восстановлена, "
                                            f"мониторинг снова работает (простой {outage}).")

    while True:
        client = TelegramClient(
            sess_cfg["session_file"],
            sess_cfg.get("api_id", cfg["api_id"]),
            sess_cfg.get("api_hash", cfg["api_hash"]),
        )
        progress = None
        wait = backoff
        attempt_started = time.time()
        try:
            # connect() itself calls get_me/GetState: Telethon's own short
            # FloodWait sleeps are fine here; a long one is obeyed below.
            me = await asyncio.wait_for(_login(client), LOGIN_TIMEOUT_SECONDS)
            if me is None:
                LOGGER.error("[%s] файловая сессия не авторизована; остановите монитор и выполните "
                             "python \"login_telethon.py\" с соответствующим login_session_name", name)
                await tech.problem(f"session:{name}",
                                   f"🔴 {who}: сессия не авторизована — мониторинг этого "
                                   f"оператора не работает. {relogin}", repeat=False)
                return
            expected = sess_cfg.get("operator_telegram_id")
            if expected is not None and me.id != expected:
                LOGGER.error("[%s] operator_telegram_id=%s, но session принадлежит Telegram ID=%s. "
                             "Проверьте session_file и login_session_name! Аккаунт не будет отслеживаться.",
                             name, expected, me.id)
                await tech.problem(f"session:{name}",
                                   f"🔴 {who}: файл сессии принадлежит Telegram ID {me.id}, а в "
                                   f"конфиге operator_telegram_id={expected}. Мониторинг этого "
                                   "оператора не запущен.", repeat=False)
                return
            if down_since is not None:
                await link_restored(down_since)
                down_since = None
            await tech.resolved(f"internal:{name}", f"🟢 {who}: подключение снова работает.")
            await tech.resolved(f"flood:{name}", f"🟢 {who}: ограничение Telegram закончилось.")
            backoff = RECONNECT_MIN_DELAY
            LOGGER.info("[%s] подключён оператор %s (Telegram ID %s)", name,
                        sess_cfg.get("operator_label", name), me.id)
            if cfg.get("notify_operator") and expected == cfg.get("main_telegram_id"):
                LOGGER.warning("[%s] ID оператора = ID главного; будет одно сообщение главному", name)
            _subscribe_read_updates(client, cfg, sess_cfg, state, name)
            # Avoid Telethon's implicit small sleeps: all FloodWaitError waits are
            # logged and obeyed explicitly, including during a history scan.
            client.flood_sleep_threshold = 0
            progress = _Progress()
            poll = asyncio.ensure_future(_poll_forever(cfg, sess_cfg, client, state, notifier, me.id,
                                                       pacer, tech, fatal_errors, flood_error, progress))
            await _watch_poll(poll, client, progress)
        except asyncio.CancelledError:
            if not _cancelled_by_telethon(client):
                raise
            if down_since is None:
                down_since = progress.outage_start() if progress else attempt_started
            LOGGER.warning("[%s] Telethon сам разорвал соединение; переподключение через %s сек.",
                           name, backoff)
        except flood_error as error:
            wait = max(1, int(error.seconds) + cfg.get("flood_wait_extra_seconds", 3))
            LOGGER.warning("[%s] FloodWait при подключении %s сек.; жду %s сек.", name, error.seconds, wait)
            if down_since is not None:  # Telegram answered: the network itself is back
                await link_restored(down_since)
                down_since = None
            if wait >= LONG_FLOOD_WAIT_SECONDS:
                until = datetime.fromtimestamp(time.time() + wait, timezone.utc)
                await tech.problem(f"flood:{name}",
                                   f"🟠 {who}: Telegram ограничил запросы при подключении (FloodWait "
                                   f"{humanize_seconds(wait)}). Мониторинг этого оператора "
                                   f"приостановлен до {format_local(until, cfg.get('utc_offset_hours', 3))}.",
                                   repeat=False)
        except fatal_errors as error:
            LOGGER.error("[%s] Telegram отклонил сессию: %s", name, type(error).__name__)
            await tech.problem(f"session:{name}",
                               f"🔴 {who}: Telegram отклонил сессию ({esc(type(error).__name__)}) — "
                               "её завершили в «Устройствах» или она недействительна. Мониторинг "
                               f"этого оператора остановлен. {relogin}", repeat=False)
            return
        except CONNECTION_ERRORS as error:
            if down_since is None:
                # Noticed while sweeping: began around the last progress;
                # while logging in: when this attempt started.
                down_since = progress.outage_start() if progress else attempt_started
            LOGGER.warning("[%s] нет связи с Telegram или запрос завис (%s%s); переподключение через %s сек.",
                           name, type(error).__name__, f": {error}" if str(error) else "", backoff)
        except Exception as error:
            LOGGER.exception("[%s] внутренняя ошибка мониторинга; переподключение через %s сек.",
                             name, backoff)
            await tech.problem(f"internal:{name}",
                               f"🔴 {who}: внутренняя ошибка мониторинга ({error_text(error)}). "
                               f"Переподключение через {backoff} сек.")
        finally:
            await _safe_disconnect(client)
        if down_since is not None and time.time() - down_since >= grace:
            since = format_local(datetime.fromtimestamp(down_since, timezone.utc),
                                 cfg.get("utc_offset_hours", 3))
            await tech.problem(f"link:{name}",
                               f"🟠 {who}: мониторинг не работает с {since} "
                               f"({humanize_seconds(time.time() - down_since)}): нет связи с Telegram "
                               "или запросы зависли. Переподключение идёт автоматически.")
        await asyncio.sleep(wait)
        if wait == backoff:
            backoff = min(RECONNECT_MAX_DELAY, backoff * 2)

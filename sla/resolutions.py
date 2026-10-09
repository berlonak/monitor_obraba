# copyright by berlonak
# telegram: @Kilax123
"""Mark delivered SLA alerts as resolved by editing the original bot messages.

No Telegram user-account API calls: only editMessageText of already delivered bot
messages. Telegram's exact read timestamp is not exposed, so unread resolution
shows the time we *noticed* a read during the next monitor pass.
"""
import asyncio
from datetime import datetime, timezone
import logging

from .timeutils import format_local

LOGGER = logging.getLogger(__name__)


def resolved_text(original_html: str, kind: str, resolved_ts: int, utc_offset_hours: int,
                  reason=None) -> str:
    if kind not in ("unread", "unanswered"):
        raise ValueError("Неизвестный тип уведомления")
    if reason == "deleted":
        # Telegram does not say WHO deleted it: the lead or the operator.
        title = "⚪️ <b>Сообщение лида удалено из чата</b>"
        timing = "Удаление обнаружено"
        old_status = "Удалить мог лид или оператор; напоминания по этому сообщению остановлены"
    elif kind == "unread":
        title = "✅ <b>Лид прочитан</b>"
        timing = "Прочтение обнаружено"
        old_status = "Не был прочитан вовремя"
    elif kind == "unanswered":
        title = "✅ <b>Лиду ответили</b>"
        timing = "Ответ отправлен"
        old_status = "Ответ был просрочен"
    else:
        raise ValueError("Неизвестный тип уведомления")
    when = format_local(datetime.fromtimestamp(resolved_ts, timezone.utc), utc_offset_hours)
    lines = original_html.split("\n")
    # Keep the message body and lead link, replacing the alarming heading.
    # Historic numbers are labelled as a snapshot, not as the CURRENT state.
    details = []
    for line in lines[1:]:
        if line.startswith("Сейчас: "):
            line = line.replace("Сейчас: ", "На момент уведомления: ", 1)
        elif line.startswith("Не прочитано уже: "):
            line = line.replace("Не прочитано уже: ", "Ожидание на тот момент: ", 1)
        elif line.startswith("Без ответа: "):
            line = line.replace("Без ответа: ", "Ожидание на тот момент: ", 1)
        elif line.startswith("Непрочитано: "):
            line = line.replace("Непрочитано: ", "Было непрочитано: ", 1)
        details.append(line)
    return "\n".join([title, f"{timing}: <b>{when}</b>", old_status, *details])


async def apply_pending_edits(state, notifier, cfg, limit=8, now_ts=None, pace_seconds=0.35):
    """Process a small bounded batch, allowing retries after HTTP/rate errors.

    Rows are stored before this worker attempts an edit and are kept across a
    monitor restart. A deleted bot message is discarded instead of retried.
    """
    now_ts = int(datetime.now(timezone.utc).timestamp()) if now_ts is None else now_ts
    # A 429 applies to bot activity generally; do not immediately try a
    # *different* pending message on the next worker tick.
    if now_ts < getattr(notifier, "_edits_paused_until", 0):
        LOGGER.debug('Редактирование отложено до %s из-за Bot API FloodWait',
                     getattr(notifier, '_edits_paused_until'))
        return 0
    rows = state.pending_edits(now_ts, limit=limit)
    if rows:
        LOGGER.info('Зелёные галочки: в очереди на этот проход %s шт.', len(rows))
    count = 0
    for row in rows:
        ident, recipient, message_id, original, kind, resolved_ts, attempts, reason = row
        text = resolved_text(original, kind, resolved_ts, cfg["utc_offset_hours"], reason)
        LOGGER.debug('Редактирование: очередь id=%s получатель=%s bot_message_id=%s '
                     'тип=%s попыток=%s', ident, recipient, message_id, kind, attempts)
        status, retry_after = await notifier.edit(recipient, message_id, text)
        LOGGER.debug('Редактирование: id=%s результат=%s retry_after=%s',
                     ident, status, retry_after)
        if status in ("edited", "gone"):
            state.complete_edit(ident)
            count += 1
            LOGGER.info('Зелёная галочка: получатель=%s сообщение=%s результат=%s',
                        recipient, message_id, status)
        else:
            # Exponential backoff on network errors, Telegram-provided delay
            # on 429. Never hammer Bot API when a message can't be edited.
            wait = retry_after or min(1800, 30 * (2 ** min(attempts, 6)))
            state.retry_edit(ident, now_ts + max(1, int(wait)))
            if status == "rate_limited":
                notifier._edits_paused_until = now_ts + max(1, int(wait))
                break
        if pace_seconds:
            await asyncio.sleep(pace_seconds)
    return count


async def edit_worker(cfg, state, notifier):
    """Runs alongside dialog polling; edits don't prolong GetHistory sweeps."""
    while True:
        try:
            await apply_pending_edits(state, notifier, cfg)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Ошибка обновления старых уведомлений; повторю")
        await asyncio.sleep(2)

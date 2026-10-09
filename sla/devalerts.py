# copyright by berlonak
# telegram: @Kilax123
"""Technical notifications for the DEVELOPER only.

Outages, broken/revoked sessions, undeliverable recipients, crashed tasks.
The supervisor (main_telegram_id) and operators never receive these messages:
they get lead alerts only. Without developer_telegram_id everything here is
written to the log only.
"""
import asyncio
from datetime import datetime, timezone
from html import escape, unescape
import logging
import re
import time

from .debuglog import redact
from .timeutils import format_local

LOGGER = logging.getLogger(__name__)
HEADER = "🛠 <b>SLA-монитор</b>"
_TAGS = re.compile(r"<[^>]+>")


def esc(value):
    return escape(str(value), quote=False)


def error_text(error, limit=300):
    """Short, HTML-safe description of an exception (secrets are redacted on send)."""
    message = str(error).strip()
    text = type(error).__name__ + (f": {message}" if message else "")
    return esc(text[:limit])


class TechAlerts:
    """Deduplicated problem/resolved notifications keyed by problem id.

    problem(key) sends once; while the problem persists it repeats only every
    tech_alert_repeat_seconds (0 = never). resolved(key) reports recovery, but
    only if the developer was actually told about the problem.
    """

    def __init__(self, cfg, notifier=None, clock=time.time):
        self.cfg = cfg
        self.notifier = notifier
        self.developer_id = cfg.get("developer_telegram_id")
        self.repeat_seconds = int(cfg.get("tech_alert_repeat_seconds", 21600) or 0)
        self.clock = clock
        self._active = {}

    def is_active(self, key):
        return key in self._active

    async def problem(self, key, text, repeat=True):
        now = self.clock()
        entry = self._active.get(key)
        if entry is None:
            entry = self._active[key] = {"since": now, "sent_at": None, "text": text}
            LOGGER.warning("Технический сбой [%s]: %s", key, self._plain(text))
        elif entry["sent_at"] is not None:
            if not (repeat and self.repeat_seconds and now - entry["sent_at"] >= self.repeat_seconds):
                return False
            text = f"{text}\n\n<i>Напоминание: проблема не устранена с {self._when(entry['since'])}.</i>"
        entry["text"] = text
        entry["attempted"] = True
        if await self._send(text):
            entry["sent_at"] = now
            return True
        return False

    async def resolved(self, key, text):
        entry = self._active.pop(key, None)
        if entry is None:
            return False
        LOGGER.info("Технический сбой устранён [%s]: %s", key, self._plain(text))
        if entry["sent_at"] is None:
            if not entry.get("attempted"):
                return False  # never reported (e.g. a short blip): no "fixed" noise
            # The report itself could not get through (typically the whole host
            # was offline): tell the developer now, or nobody ever learns it.
            text = (f"{text}\n\n<i>Во время сбоя сообщить не удалось (не было связи). "
                    f"Сбой начался {self._when(entry['since'])}:</i>\n{entry['text']}")
        return await self._send(text)

    def _when(self, ts):
        return format_local(datetime.fromtimestamp(ts, timezone.utc), self.cfg.get("utc_offset_hours", 3))

    async def info(self, text):
        LOGGER.info("Техническое уведомление: %s", self._plain(text))
        return await self._send(text)

    async def _send(self, text, retry_delays=(2, 5)):
        if not self.developer_id or self.notifier is None:
            return False
        body = f"{HEADER}\n{redact(self.cfg, text)}"
        status = "transient"
        for attempt in range(len(retry_delays) + 1):
            if attempt:
                await asyncio.sleep(retry_delays[attempt - 1])
            try:
                deliver = getattr(self.notifier, "deliver", None)
                if deliver is not None:
                    status = (await deliver(self.developer_id, body)).status
                else:
                    status = "ok" if await self.notifier.send(self.developer_id, body) else "transient"
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.error("Техническое уведомление разработчику не отправлено (%s)", type(error).__name__)
                status = "transient"
            if status != "transient":
                break
        if status != "ok":
            LOGGER.error("Техническое уведомление разработчику (Telegram ID %s) не доставлено%s",
                         self.developer_id,
                         ": он должен открыть бота и нажать /start" if status == "permanent" else "")
        return status == "ok"

    @staticmethod
    def _plain(text):
        return unescape(_TAGS.sub("", text)).replace("\n", " ")


async def supervise(title, factory, tech=None, restart_delay=30):
    """Run a background task; a crash is reported to the developer and the task restarts.

    A normal return (for example an operator whose session is unusable and was
    already reported) ends supervision without a restart.
    """
    while True:
        try:
            await factory()
            return
        except asyncio.CancelledError:
            task = asyncio.current_task()
            cancelling = getattr(task, "cancelling", None)
            if cancelling is None or cancelling():
                raise  # a real shutdown
            error = RuntimeError("задача прервана изнутри (CancelledError не от остановки процесса)")
            LOGGER.error("Задача «%s» прервана изнутри; перезапуск через %s сек.", title, restart_delay)
            if tech is not None:
                await tech.problem(f"task:{title}",
                                   f"🔴 Внутренняя ошибка в задаче «{esc(title)}»: {error_text(error)}. "
                                   f"Перезапуск через {restart_delay} сек.")
            await asyncio.sleep(restart_delay)
        except Exception as error:
            LOGGER.exception("Задача «%s» упала; перезапуск через %s сек.", title, restart_delay)
            if tech is not None:
                await tech.problem(f"task:{title}",
                                   f"🔴 Внутренняя ошибка в задаче «{esc(title)}»: {error_text(error)}. "
                                   f"Перезапуск через {restart_delay} сек.")
            await asyncio.sleep(restart_delay)

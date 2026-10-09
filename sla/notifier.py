# copyright by berlonak
# telegram: @Kilax123
"""HTTP Telegram Bot API delivery (only chat ID is needed; no access_hash cache)."""
import asyncio
from collections import namedtuple
import logging
import time
from io import BytesIO

import aiohttp

LOGGER = logging.getLogger(__name__)

# status: "ok"; "permanent" — Telegram refused this recipient/message (403: bot
# blocked or /start never pressed; 400: chat not found, bad request) and an
# identical retry cannot succeed; "transient" — network, 5xx, 429: retry later.
Delivery = namedtuple("Delivery", "status message_id description")


class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg
        self.http = None
        self._base_url = "https://api.telegram.org/bot" + cfg["notifier_bot_token"] + "/"
        self._send_paused_until = 0.0  # time.monotonic(); Bot API 429 retry_after

    async def start(self, max_delay=300):
        """Connect to Bot API. Network problems are retried forever (a monitor
        started before the network is up must not exit); a rejected token is fatal."""
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
        delay = 5
        while True:
            try:
                response = await self.request("getMe")
            except asyncio.CancelledError:
                await self.stop()
                raise
            except Exception as error:
                # An aiohttp exception can contain the full token in its URL.
                LOGGER.warning("Нет соединения с Telegram Bot API (%s); повтор через %s сек.",
                               type(error).__name__, delay)
            else:
                if response.get("ok"):
                    LOGGER.info("Бот @%s готов", response["result"].get("username", "unknown"))
                    return
                if response.get("error_code") in (401, 404):
                    await self.stop()
                    raise ValueError("Бот не авторизован: проверьте notifier_bot_token")
                LOGGER.warning("Bot API getMe: %s; повтор через %s сек.",
                               response.get("description", "ошибка"), delay)
            await asyncio.sleep(delay)
            delay = min(max_delay, delay * 2)

    async def request(self, method, data=None):
        """An HTTP call; callers must handle ok=False. Never log the secret URL."""
        if self.http is None:
            raise RuntimeError("Notifier.start() ещё не вызывался")
        started = time.monotonic()
        LOGGER.debug('Bot API вызов %s: получатель=%s message_id=%s', method,
                     (data or {}).get('chat_id'), (data or {}).get('message_id'))
        async with self.http.post(self._base_url + method, json=data or {}) as response:
            payload = await response.json(content_type=None)
            LOGGER.debug('Bot API ответ %s: HTTP %s ok=%s error_code=%s '
                         'время=%.2f сек.', method, response.status,
                         payload.get('ok'), payload.get('error_code'),
                         time.monotonic() - started)
            return payload

    async def deliver(self, telegram_id, message):
        """Send one HTML message and classify the outcome (see Delivery)."""
        wait = self._send_paused_until - time.monotonic()
        if wait > 0:
            return Delivery("transient", None, f"Bot API 429: пауза ещё {int(wait) + 1} сек.")
        try:
            result = await self.request("sendMessage", {
                "chat_id": int(telegram_id),
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # aiohttp errors may include the token-bearing URL; never print the exception!
            LOGGER.error("Ошибка сети Bot API для Telegram ID %s (%s). Повторю позже.",
                         telegram_id, type(error).__name__)
            return Delivery("transient", None, type(error).__name__)
        if result.get("ok"):
            message_id = (result.get("result") or {}).get("message_id")
            LOGGER.debug('Bot API отправлено: получатель=%s message_id=%s', telegram_id, message_id)
            if not isinstance(message_id, int):
                # Delivered: must NOT be re-sent, although it cannot be edited later.
                LOGGER.warning("Бот доставил уведомление %s, но не вернул message_id", telegram_id)
                message_id = None
            return Delivery("ok", message_id, None)
        code = result.get("error_code")
        description = str(result.get("description") or "неизвестная ошибка")
        if code == 429:
            retry_after = (result.get("parameters") or {}).get("retry_after")
            pause = int(retry_after) + 1 if isinstance(retry_after, int) else 30
            self._send_paused_until = time.monotonic() + pause
            LOGGER.warning("Bot API ограничил отправку (429): пауза %s сек., остальное — на следующем проходе",
                           pause)
            return Delivery("transient", None, description)
        if code in (400, 403):
            LOGGER.error("Бот не может доставить сообщение Telegram ID %s: %s", telegram_id, description)
            return Delivery("permanent", None, description)
        LOGGER.error("Бот не доставил сообщение Telegram ID %s: %s. Повторю позже.", telegram_id, description)
        return Delivery("transient", None, description)

    async def send_tracked(self, telegram_id, message):
        """Return (delivered, bot_message_id); keep IDs for future green marks."""
        result = await self.deliver(telegram_id, message)
        return result.status == "ok", result.message_id

    async def send(self, telegram_id, message):
        return (await self.deliver(telegram_id, message)).status == "ok"

    async def edit(self, telegram_id, message_id, message):
        """Return (edited|gone|retry|rate_limited, wait_seconds_or_None)."""
        try:
            result = await self.request("editMessageText", {
                "chat_id": int(telegram_id),
                "message_id": int(message_id),
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.warning("Ошибка изменения уведомления %s/%s (%s)",
                           telegram_id, message_id, type(error).__name__)
            return "retry", None
        if result.get("ok"):
            LOGGER.debug('Bot API editMessageText успешно: %s/%s', telegram_id, message_id)
            return "edited", None
        description = str(result.get("description", "")).lower()
        if "message is not modified" in description:
            return "edited", None
        retry_after = (result.get("parameters") or {}).get("retry_after")
        if result.get("error_code") == 429:
            wait = int(retry_after) + 1 if isinstance(retry_after, int) else 60
            LOGGER.warning("Bot API ограничил изменения уведомлений: пауза %s сек.", wait)
            return "rate_limited", wait
        # 400/403 are terminal: message deleted/not editable, chat not found,
        # bot blocked. Retrying the identical request can never succeed.
        if result.get("error_code") in (400, 403) or any(marker in description for marker in (
            "message to edit not found", "message can't be edited", "message_id_invalid",
            "message text is empty", "bot was blocked", "chat not found", "user is deactivated",
        )):
            LOGGER.info("Исходное уведомление %s/%s нельзя изменить: %s",
                        telegram_id, message_id, description)
            return "gone", None
        LOGGER.warning("Не получилось изменить уведомление %s/%s: %s",
                       telegram_id, message_id, description)
        return "retry", None

    async def send_document(self, telegram_id, filename, data):
        """Send an in-memory Excel file; the token must never appear in logs."""
        if self.http is None:
            return False
        try:
            form = aiohttp.FormData()
            form.add_field('chat_id', str(int(telegram_id)))
            form.add_field('document', BytesIO(data), filename=filename,
                           content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            async with self.http.post(self._base_url + 'sendDocument', data=form) as response:
                result = await response.json(content_type=None)
            if not result.get('ok'):
                LOGGER.warning('Bot API: не удалось отправить Excel: %s', result.get('description', 'ошибка'))
            return bool(result.get('ok'))
        except Exception as error:
            LOGGER.warning('Не удалось отправить Excel (%s)', type(error).__name__)
            return False

    async def stop(self):
        if self.http is not None:
            await self.http.close()
            self.http = None

# copyright by berlonak
# telegram: @Kilax123
"""Escaped HTML alerts with the correct distinction between unread and unanswered."""
from datetime import datetime, timezone
from html import escape

from .timeutils import format_local, humanize_seconds


def build_alerts(entity, operator_label, first_ts, latest_message, unread, offset, preview_len, now,
                 kind="unanswered"):
    if kind not in ("unanswered", "unread"):
        raise ValueError("kind must be unanswered or unread")
    name = " ".join(filter(None, [getattr(entity, "first_name", None), getattr(entity, "last_name", None)])) or "—"
    username = getattr(entity, "username", None)
    contact = bool(getattr(entity, "contact", False))
    recent_text = (getattr(latest_message, "message", "") or "").strip().replace("\n", " ")
    if preview_len == 0:
        recent_text = "[превью отключено]"
    elif len(recent_text) > preview_len:
        recent_text = recent_text[:preview_len] + "…"
    if not recent_text:
        recent_text = ("[медиа/нет текста]" if getattr(latest_message, "media", None)
                       else "[нет доступного превью]")
    user_link = f'<a href="tg://user?id={int(entity.id)}">открыть чат</a>'
    if username:
        user_link += f' · <a href="https://t.me/{escape(username, quote=True)}">@{escape(username)}</a>'
    first_dt = datetime.fromtimestamp(first_ts, tz=timezone.utc)
    unread_mode = kind == "unread"
    lead_lines = [
        f"Лид: <b>{escape(name)}</b>" + (" (контакт)" if contact else ""),
        f"Username: {escape('@' + username) if username else '—'}",
        f"Telegram ID: <code>{int(entity.id)}</code>",
        ("Первое непрочитанное сообщение: " if unread_mode else "Первое сообщение без ответа: ")
        + format_local(first_dt, offset),
        f"Сейчас: {format_local(now, offset)}",
        ("Не прочитано уже: " if unread_mode else "Без ответа: ")
        + f"<b>{humanize_seconds((now - first_dt).total_seconds())}</b>",
        f"Непрочитано: {unread}",
        f"Сообщение лида: {escape(recent_text)}",
        user_link,
    ]
    if unread_mode:
        main_heading, op_heading = "⚠️ <b>Лид не прочитан вовремя</b>", "🔔 <b>У тебя непрочитанный лид</b>"
    else:
        main_heading, op_heading = "⚠️ <b>Просрочен ответ лиду</b>", "🔔 <b>У тебя лид без ответа</b>"
    main = "\n".join([main_heading, f"Оператор: <b>{escape(operator_label)}</b>", *lead_lines])
    operator = "\n".join([op_heading, *lead_lines])
    return main, operator

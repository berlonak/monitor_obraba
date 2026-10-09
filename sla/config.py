# copyright by berlonak
# telegram: @Kilax123
"""YAML configuration and validation, independent of Telegram."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Optional

import yaml


@dataclass(frozen=True)
class QuietHours:
    """Minutes from midnight in the configured fixed UTC offset."""
    start_minute: int
    end_minute: int


DEFAULTS = {
    "sla_seconds": 3600,
    "alert_mode": "unanswered",  # unanswered | unread | both
    "stats_enabled": True,
    "edit_resolved_alerts": True,
    "debug_logging": False,
    "debug_log_file": "logs/sla_debug.log",
    "developer_telegram_id": None,      # technical alerts go ONLY here
    "tech_alert_delay_seconds": 300,    # outage must last this long before alerting
    "tech_alert_repeat_seconds": 21600, # repeat while unresolved; 0 = never
    "history_max_age_hours": 72,        # older incoming messages never open a NEW case; 0 = no limit
    "poll_interval_seconds": 60,
    "utc_offset_hours": 3,
    "quiet_hours": 0,
    "remind_every_seconds": 600,
    "max_reminders": 0,
    "notify_operator": True,
    "only_contacts": False,
    "include_archived": False,
    "include_ids": [],
    "exclude_ids": [],
    "message_preview_len": 160,
    "history_scan_limit": 100,
    "unread_scan_limit": 200,
    "history_request_delay_seconds": 3.2,
    "history_page_delay_seconds": 3.2,
    "flood_wait_extra_seconds": 3,
    "alert_existing_chats_on_start": True,
    "state_db": "sla_state.sqlite3",
    "sessions": [],
    "login_session_name": None,
}


def _int(value, name, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name}: требуется целое число")
    if value < minimum or (maximum is not None and value > maximum):
        limit = f" до {maximum}" if maximum is not None else ""
        raise ValueError(f"{name}: требуется число от {minimum}{limit}")
    return value


def _bool(value, name):
    if not isinstance(value, bool):
        raise ValueError(f"{name}: используйте true или false (без кавычек)")
    return value


def _telegram_id(value, name):
    return _int(value, name, minimum=1)


def _clock(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError(f'{name}: формат времени "HH:MM" (например, "23:00")')
    hh, mm = map(int, value.split(":"))
    return hh * 60 + mm


def parse_quiet_hours(value) -> Optional[QuietHours]:
    """0 (numeric) disables suppression; {start: HH:MM, end: HH:MM} enables it."""
    if type(value) is int and value == 0:
        return None
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise ValueError('quiet_hours: укажите 0 или {start: "23:00", end: "09:00"}')
    start = _clock(value["start"], "quiet_hours.start")
    end = _clock(value["end"], "quiet_hours.end")
    if start == end:
        raise ValueError("quiet_hours: start и end совпадают; для отключения используйте 0")
    return QuietHours(start, end)


def _alias(data, modern, legacy, location, required=False):
    a, b = data.get(modern), data.get(legacy)
    if a is not None and b is not None and a != b:
        raise ValueError(f"{location}: одновременно заданы разные {modern} и {legacy}")
    value = a if a is not None else b
    if required and value is None:
        raise ValueError(f"{location}: обязательно укажите {modern}")
    return value


def resolve_session_path(value, config_dir, location):
    """Return an absolute SQLiteSession name; Telethon adds .session when needed.

    The same resolver is used by the UNMODIFIED external login script's
    config.py adapter and by the running monitor, so both open one file.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location}: укажите путь/имя файловой сессии Telethon")
    if "\0" in value:
        raise ValueError(f"{location}: путь содержит недопустимый символ")
    path = Path(value.strip()).expanduser()
    path = path if path.is_absolute() else Path(config_dir) / path
    return str(path.resolve())


def session_database_path(session_name):
    """Concrete filename that Telethon's SQLiteSession will open."""
    return Path(session_name if session_name.endswith(".session") else session_name + ".session")


def load_login_settings(config_path):
    """Read only the three fields required by the user's original login script.

    Login is possible before bot token, supervisor ID and operator list are set.
    """
    path = Path(config_path).expanduser().resolve()
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except yaml.YAMLError as exc:
        raise ValueError("config.yaml: ошибка YAML (проверьте кавычки и отступы)") from exc
    if not isinstance(data, dict):
        raise ValueError("config.yaml: ожидается YAML-словарь")
    api_id = _int(data.get("api_id"), "api_id", minimum=1)
    api_hash = data.get("api_hash")
    if not isinstance(api_hash, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
        raise ValueError("api_hash: вставьте реальный 32-значный ключ из my.telegram.org")
    session_name = resolve_session_path(data.get("login_session_name"), path.parent, "login_session_name")
    session_database_path(session_name).parent.mkdir(parents=True, exist_ok=True)
    return api_id, api_hash, session_name


def load_config(path, command="run"):
    """Validate config; whoami and the unchanged external login have separate entry paths."""
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            original = yaml.safe_load(handle)
    except yaml.YAMLError as error:
        raise ValueError("config.yaml: ошибка формата YAML; проверьте отступы и кавычки") from error
    if not isinstance(original, dict):
        raise ValueError("Корень config.yaml должен быть YAML-словарём")
    cfg = {**DEFAULTS, **original}

    cfg["api_id"] = _int(cfg.get("api_id"), "api_id", minimum=1)
    if not isinstance(cfg.get("api_hash"), str) or not re.fullmatch(r"[a-fA-F0-9]{32}", cfg["api_hash"]):
        raise ValueError("api_hash: вставьте реальный 32-значный ключ из my.telegram.org")

    for field, min_value, max_value in (
        ("sla_seconds", 1, None),
        ("poll_interval_seconds", 10, None),
        ("utc_offset_hours", -12, 14),
        ("remind_every_seconds", 0, None),
        ("max_reminders", 0, None),
        ("message_preview_len", 0, 1000),
        ("history_scan_limit", 10, 10000),
        ("unread_scan_limit", 10, 5000),
        ("flood_wait_extra_seconds", 0, 300),
        ("tech_alert_delay_seconds", 0, 86400),
        ("tech_alert_repeat_seconds", 0, 604800),
        ("history_max_age_hours", 0, 87600),
    ):
        cfg[field] = _int(cfg[field], field, min_value, max_value)

    mode = cfg["alert_mode"]
    if mode not in ("unanswered", "unread", "both"):
        raise ValueError("alert_mode: укажите unanswered, unread или both")
    for field in ("history_request_delay_seconds", "history_page_delay_seconds"):
        val = cfg[field]
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not 0 <= val <= 60:
            raise ValueError(f"{field}: укажите число от 0 до 60 секунд")
        cfg[field] = float(val)
    for field in ("notify_operator", "only_contacts", "include_archived", "alert_existing_chats_on_start", "stats_enabled", "edit_resolved_alerts", "debug_logging"):
        cfg[field] = _bool(cfg[field], field)

    cfg["quiet_window"] = parse_quiet_hours(cfg["quiet_hours"])
    for field in ("include_ids", "exclude_ids"):
        if not isinstance(cfg[field], list):
            raise ValueError(f"{field}: укажите список ID, например []")
        cfg[field] = [_telegram_id(i, f"{field}[]") for i in cfg[field]]

    cfg["main_telegram_id"] = _alias(cfg, "main_telegram_id", "main_id", "главный")
    if cfg["main_telegram_id"] is not None:
        cfg["main_telegram_id"] = _telegram_id(cfg["main_telegram_id"], "main_telegram_id")
    if cfg["developer_telegram_id"] is not None:
        cfg["developer_telegram_id"] = _telegram_id(cfg["developer_telegram_id"], "developer_telegram_id")
    if not isinstance(cfg["state_db"], str) or not cfg["state_db"].strip():
        raise ValueError("state_db: укажите путь к SQLite-файлу")
    # Relative paths are resolved next to config.yaml, not the current shell directory.
    db_path = Path(cfg["state_db"])
    cfg["state_db"] = str(db_path if db_path.is_absolute() else path.resolve().parent / db_path)
    log_file = cfg.get("debug_log_file")
    if not isinstance(log_file, str) or not log_file.strip() or "\0" in log_file:
        raise ValueError("debug_log_file: укажите путь к файлу журнала")
    log_path = Path(log_file.strip()).expanduser()
    cfg["debug_log_file"] = str(
        (log_path if log_path.is_absolute() else path.resolve().parent / log_path).resolve()
    )
    if cfg["debug_log_file"] == str(Path(cfg["state_db"]).resolve()):
        raise ValueError("debug_log_file: нельзя использовать файл базы данных")

    if cfg["login_session_name"] is not None:
        cfg["login_session_name"] = resolve_session_path(
            cfg["login_session_name"], path.resolve().parent, "login_session_name"
        )

    if not isinstance(cfg["sessions"], list):
        raise ValueError("sessions: требуется список операторов")
    seen = set()
    seen_files = set()
    normalized = []
    for n, session in enumerate(cfg["sessions"], 1):
        loc = f"sessions[{n}]"
        if not isinstance(session, dict):
            raise ValueError(f"{loc}: требуется YAML-словарь")
        item = dict(session)
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{loc}.name: обязательная уникальная строка")
        if name in seen:
            raise ValueError(f"{loc}.name: имя {name!r} уже используется")
        seen.add(name)
        label = item.get("operator_label", name)
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"{loc}.operator_label: укажите имя")
        item["operator_label"] = label
        item["operator_telegram_id"] = _alias(item, "operator_telegram_id", "operator_id", loc)
        if item["operator_telegram_id"] is not None:
            item["operator_telegram_id"] = _telegram_id(item["operator_telegram_id"], f"{loc}.operator_telegram_id")
        # Every monitored operator must have a unique file-based Telethon
        # session, produced by login_telethon.py.
        if "session_file" in item:
            item["session_file"] = resolve_session_path(
                item["session_file"], path.resolve().parent, f"{loc}.session_file"
            )
            db_file = session_database_path(item["session_file"])
            if db_file in seen_files:
                raise ValueError(f"{loc}.session_file: одна файловая сессия назначена двум операторам")
            seen_files.add(db_file)
        if "api_id" in item:
            item["api_id"] = _int(item["api_id"], f"{loc}.api_id", minimum=1)
        if "api_hash" in item and (not isinstance(item["api_hash"], str) or not item["api_hash"].strip()):
            raise ValueError(f"{loc}.api_hash: нужна непустая строка")
        normalized.append(item)
    cfg["sessions"] = normalized
    # Chats between staff (supervisor, developer, operators) are never leads.
    cfg["_staff_ids"] = frozenset(
        value for value in [cfg["main_telegram_id"], cfg["developer_telegram_id"],
                            *(item.get("operator_telegram_id") for item in normalized)]
        if isinstance(value, int))

    if command in ("run", "check", "whoami"):
        if not isinstance(cfg.get("notifier_bot_token"), str) or not re.fullmatch(
            r"[0-9]+:[A-Za-z0-9_-]{20,}", cfg["notifier_bot_token"]
        ):
            raise ValueError("notifier_bot_token: вставьте полный реальный токен бота из @BotFather")
    if command in ("run", "check"):
        if cfg["main_telegram_id"] is None:
            raise ValueError("main_telegram_id: укажите Telegram ID руководителя")
        if not cfg["sessions"]:
            raise ValueError("sessions: добавьте хотя бы одного оператора")
        for n, item in enumerate(cfg["sessions"], 1):
            if not item.get("session_file"):
                raise ValueError(f"sessions[{n}].session_file: укажите путь к сессии вашей логинилки")
            if cfg["notify_operator"] and item["operator_telegram_id"] is None:
                raise ValueError(f"sessions[{n}].operator_telegram_id: задайте ID оператора")
    return cfg

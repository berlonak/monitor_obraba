#!/usr/bin/env python3
# copyright by berlonak
# telegram: @Kilax123
"""CLI: python sla_monitor.py {check,whoami,run} [-c config.yaml]."""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

from sla import __version__
from sla.config import load_config
from sla.instance_lock import AlreadyRunning, InstanceLock, lock_path_for
from sla.state import State

LOGGER = logging.getLogger("sla_monitor")


async def whoami(cfg):
    from sla.notifier import Notifier

    # The running monitor polls getUpdates for /stats; two consumers conflict.
    with InstanceLock(lock_path_for(cfg)):
        bot = Notifier(cfg)
        await bot.start()
        offset = 0
        try:
            # A setup run must not reply to everybody's historical messages.
            # Drop previously queued updates; only answer people writing right now.
            old_updates = await bot.request("getUpdates", {
                "offset": -1, "timeout": 0, "allowed_updates": ["message"]
            })
            if old_updates.get("ok") and old_updates.get("result"):
                offset = max(event["update_id"] for event in old_updates["result"]) + 1
            print("Отправьте вашему боту сообщение в Telegram. Он ответит вашим числовым ID. Ctrl+C — выход.")
            while True:
                # Only while `run` is stopped (the instance lock guarantees it).
                result = await bot.request("getUpdates", {
                    "offset": offset, "timeout": 25, "allowed_updates": ["message"]
                })
                if not result.get("ok"):
                    raise RuntimeError("Bot API getUpdates: " + result.get("description", "ошибка"))
                for update in result["result"]:
                    offset = max(offset, update["update_id"] + 1)
                    message = update.get("message", {})
                    chat = message.get("chat", {})
                    user = message.get("from", {})
                    if chat.get("type") != "private" or user.get("is_bot"):
                        continue
                    uid = user.get("id")
                    print(f"Telegram ID: {uid} ({user.get('first_name', '—')})")
                    await bot.send(uid, f"Ваш Telegram ID: {uid}")
        finally:
            await bot.stop()


async def run(cfg):
    from sla.devalerts import TechAlerts, error_text, supervise
    from sla.monitor import history_cutoff, monitor_session
    from sla.notifier import Notifier
    from sla.resolutions import edit_worker
    from sla.stats_bot import StatsBot

    lock = InstanceLock(lock_path_for(cfg)).acquire()
    state = None
    notifier = None
    tech = None
    stop_text = "⏹ SLA-монитор остановлен."
    try:
        state = State(cfg["state_db"])
        notifier = Notifier(cfg)
        await notifier.start()  # waits for the network instead of exiting
        tech = TechAlerts(cfg, notifier)
        state.expire_ancient_cases(history_cutoff(cfg))
        await tech.info(f"▶️ SLA-монитор v{__version__} запущен. Операторов в конфиге: "
                        f"{len(cfg['sessions'])}. Сюда приходят только технические сообщения.")
        tasks = []
        for session in cfg["sessions"]:
            title = f"оператор {session.get('operator_label', session['name'])}"
            tasks.append(supervise(title, lambda s=session: monitor_session(cfg, s, state, notifier, tech), tech))
        if cfg["stats_enabled"]:
            tasks.append(supervise("бот статистики", lambda: StatsBot(cfg, state, notifier, tech).poll(), tech))
        if cfg.get("edit_resolved_alerts", True):
            tasks.append(supervise("правка уведомлений", lambda: edit_worker(cfg, state, notifier), tech))
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        stop_text = f"🔴 SLA-монитор аварийно остановлен: {error_text(error)}"
        raise
    finally:
        if tech is not None:
            try:
                await asyncio.wait_for(tech.info(stop_text), 10)
            except (Exception, asyncio.CancelledError):
                pass
        if notifier is not None:
            await notifier.stop()
        if state is not None:
            state.close()
        lock.release()


def check(cfg):
    quiet = cfg["quiet_window"]
    raw_quiet = cfg["quiet_hours"]
    quiet_text = (
        "ОТКЛЮЧЕНО (0; круглосуточные уведомления)"
        if quiet is None else f"{raw_quiet['start']}–{raw_quiet['end']} UTC{cfg['utc_offset_hours']:+d}"
    )
    rem = cfg["remind_every_seconds"]
    repeats = (
        "ВЫКЛЮЧЕНЫ" if rem == 0 else
        f"каждые {rem} сек., " +
        ("без ограничения числа повторов" if cfg["max_reminders"] == 0 else f"максимум {cfg['max_reminders']}")
    )
    modes = {"unanswered": "без ответа", "unread": "непрочитанные", "both": "оба (два независимых уведомления)"}
    print(f"SLA-монитор v{__version__}")
    print(f"Конфиг OK. Режим: {modes[cfg['alert_mode']]}; "
          f"SLA: {cfg['sla_seconds']} сек.; опрос: {cfg['poll_interval_seconds']} сек.")
    print(f"Анти-FloodWait: между сканированиями истории {cfg['history_request_delay_seconds']} сек.; "
          f"между страницами истории {cfg['history_page_delay_seconds']} сек.; "
          f"добавлять к FloodWait {cfg['flood_wait_extra_seconds']} сек.")
    print(f"Тихие часы: {quiet_text}; повторы: {repeats}")
    print(f"Главный Telegram ID: {cfg['main_telegram_id']}; оповещать оператора: {cfg['notify_operator']}")
    developer = cfg.get("developer_telegram_id")
    if developer:
        repeat = cfg["tech_alert_repeat_seconds"]
        print(f"Технические уведомления: только разработчику, Telegram ID {developer}; "
              f"о пропаже связи — если она длится {cfg['tech_alert_delay_seconds']} сек.; "
              f"повтор: {'нет' if not repeat else f'каждые {repeat} сек.'}; "
              "разработчик должен один раз нажать /start у бота.")
    else:
        print("ВНИМАНИЕ: developer_telegram_id не задан — о сбоях (нет связи, сессия отозвана, "
              "бот заблокирован) никто не узнает, они пишутся только в журнал.")
    hours = cfg["history_max_age_hours"]
    print(f"Окно истории: {'без ограничения' if not hours else f'{hours} ч'} — "
          "более старые входящие не открывают новое обращение.")
    print(f"Статистика: {'включена, /stats и /stats_excel у главного и разработчика' if cfg['stats_enabled'] else 'выключена'}")
    print(f"DEBUG: {'включён, файл: '+cfg['debug_log_file'] if cfg['debug_logging'] else 'выключен'}")
    print(f"Пометки об обработке в старых уведомлениях: {'включены' if cfg.get('edit_resolved_alerts', True) else 'выключены'}")
    print(f"При первом запуске учитывать старые диалоги: {cfg['alert_existing_chats_on_start']}")
    print("Фильтр: только личные переписки с лидами; группы, каналы, боты, удалённые, служебный "
          "Telegram (777000) и чаты сотрудников (главный, разработчик, операторы) исключены.")
    if cfg.get("login_session_name"):
        print(f"Путь логинилки: {cfg['login_session_name']}")
    from sla.config import session_database_path
    for session in cfg["sessions"]:
        present = "ЕСТЬ" if session_database_path(session["session_file"]).is_file() else "НЕ НАЙДЕН: сначала выполните логинилку"
        print(f"Оператор: {session['operator_label']} [{session['name']}] "
              f"Telegram ID={session['operator_telegram_id']}; "
              f"сессия={session_database_path(session['session_file'])} ({present})")
    login_script = Path(__file__).resolve().with_name("login_telethon.py")
    if login_script.is_file():
        print("Оригинальная логинилка login_telethon.py: НАЙДЕНА")
    else:
        print("ВНИМАНИЕ: login_telethon.py отсутствует рядом со sla_monitor.py. "
              "Для повторной авторизации скопируйте туда ваш неизменённый файл.")
    print("Это проверка настроек; соединение с Telegram не проверялось.")


def main():
    parser = argparse.ArgumentParser(description="Контроль SLA лидов в Telegram")
    parser.add_argument("command", choices=("check", "whoami", "run"))
    parser.add_argument("-c", "--config", default="config.yaml", help="путь к config.yaml")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(args.config, command=args.command)
        from sla.debuglog import configure_logging
        configure_logging(cfg)
        if args.command == "check":
            check(cfg)
        else:
            asyncio.run({"whoami": whoami, "run": run}[args.command](cfg))
    except AlreadyRunning as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"ОШИБКА КОНФИГУРАЦИИ: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ОШИБКА ФАЙЛА: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

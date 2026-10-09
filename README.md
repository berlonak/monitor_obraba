# monitor_obraba: Telegram SLA Monitor

Monitors operators' private Telegram dialogs (Telethon user sessions) and notifies a manager
through a bot when a lead's message stays **unread** or **unanswered** longer than the SLA. It
keeps response statistics with Excel export and sends technical alerts only to the developer.

Current version: **0.5.5/0.5.6**. The full Russian guide and change history are in
[README.ru.md](README.ru.md).

## Features

- **SLA modes**: `unread` (based on Telegram's confirmed read position, not the manual
  "mark as unread" flag), `unanswered`, or `both`. Configurable SLA, polling interval, reminder
  interval and reminder limit.
- **Alerts** to the manager (`main_telegram_id`) and optionally to the operator.
  - Sent alerts get a green check with the time when the lead is read or answered.
  - Quiet hours suppress notifications but not monitoring.
- **Dialog filtering**: private chats only. Optional contacts-only mode and archived chats.
  `include_ids`/`exclude_ids`, a maximum message age and automatic exclusion of staff chats and
  777000.
- **Multiple operators**: one Telethon session per operator.
- **Statistics bot** (manager and developer, private chat only):
  - `/stats` with Today / Yesterday / 7 / 30 days and per-operator views;
  - a list of currently overdue dialogs;
  - custom date ranges;
  - `/stats_excel` export.

  Metrics: new requests, unique leads, answers, SLA breaches, share answered within an hour,
  mean/median/P90 response time, unread periods.
- **Technical alerts to the developer only**:
  - start/stop/crash;
  - missing or revoked sessions;
  - connectivity loss and recovery;
  - undeliverable notifications;
  - long FloodWait;
  - second instance (409 Conflict);
  - auto-archive misconfiguration;
  - background task errors.
- **Reliability**: FloodWait handling with configurable delays, single-instance lock, SQLite
  state with automatic migration and a rotating debug log.

## Tech Stack

- Python 3
- Telethon, aiohttp (Bot API), PyYAML, openpyxl
- SQLite

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml     # then edit config.yaml
python login_telethon.py               # authorize each operator session
```

`login_telethon.py` is the original login script and must stay byte-for-byte unchanged (a
test checks its hash). `config.py` feeds it `API_ID`, `API_HASH` and `TELETHON_SESSION_NAME`
from `config.yaml`. To authorize another operator, temporarily change `login_session_name`.

## Configuration

`config.yaml` (not committed; see the commented `config.example.yaml`):

- `api_id`, `api_hash`: Telegram API credentials.
- `notifier_bot_token`: notification bot token.
- `main_telegram_id`, `notify_operator`: alert recipients.
- `developer_telegram_id`, `tech_alert_delay_seconds`, `tech_alert_repeat_seconds`: technical alerts.
- `alert_mode`, `sla_seconds`, `poll_interval_seconds`, `remind_every_seconds`, `max_reminders`.
- `utc_offset_hours`, `quiet_hours`.
- `only_contacts`, `include_archived`, `include_ids`, `exclude_ids`, `history_max_age_hours`
  and scan limits.
- FloodWait delays, statistics switches, `debug_logging`, `state_db`.
- `sessions`: list of operators (`name`, `operator_label`, `operator_telegram_id`, `session_file`).

Recipients must send `/start` to the bot once. The config path can be overridden with `SLA_CONFIG`.

## Usage

```bash
python sla_monitor.py check     # validate config and sessions
python sla_monitor.py whoami    # show the Telegram ID of the session
python sla_monitor.py run       # start monitoring
```

## Testing

```bash
python -m unittest discover -s tests -v     # or: python -m pytest tests
```

## Project Structure

```
sla_monitor.py      CLI entry point (check / whoami / run)
sla/
  monitor.py        dialog scanning and SLA logic
  notifier.py       Bot API notifications
  devalerts.py      developer technical alerts
  stats_bot.py      statistics bot commands
  statistics.py, stats_excel.py   metrics and Excel export
  state.py          SQLite state and migrations
  resolutions.py    read/answer resolution and green checks
  config.py, formatting.py, timeutils.py, instance_lock.py, debuglog.py
config.py, login_telethon.py    login adapter and the original login script
tests/              unit tests
```

## Notes

`config.yaml`, `sessions/`, `*.session*`, `*.sqlite3*` and `logs/` are excluded from Git. Session
files give full access to operator accounts, and the debug log contains dialog and message IDs.

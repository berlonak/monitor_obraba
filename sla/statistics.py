# copyright by berlonak
# telegram: @Kilax123
"""Simple persistent case statistics. No Telegram calls: fed by the normal monitor."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from statistics import mean, median

from .timeutils import local_timezone


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS stats_answers (
       session TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
       opened_ts INTEGER NOT NULL, closed_ts INTEGER,
       PRIMARY KEY(session, chat_id, message_id))""",
    """CREATE INDEX IF NOT EXISTS stats_answers_period ON stats_answers(session, opened_ts, closed_ts)""",
    """CREATE TABLE IF NOT EXISTS stats_reads (
       session TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
       opened_ts INTEGER NOT NULL, closed_ts INTEGER,
       PRIMARY KEY(session, chat_id, message_id))""",
    """CREATE INDEX IF NOT EXISTS stats_reads_period ON stats_reads(session, opened_ts, closed_ts)""",
)


def install(conn):
    for sql in SCHEMA:
        conn.execute(sql)
    # v0.5.5: a case closed because its message was deleted / the chat vanished.
    # It still counts as an SLA breach, but never as an answer or a read.
    for table in ('stats_answers', 'stats_reads'):
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        if 'withdrawn' not in columns:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN withdrawn INTEGER NOT NULL DEFAULT 0')
    conn.commit()


def _table(kind):
    if kind not in ('answers', 'reads'):
        raise ValueError('Unknown statistics type')
    return 'stats_' + kind


def open_case(conn, kind, session, chat_id, message_id, opened_ts):
    if message_id is None or opened_ts is None:
        return
    conn.execute(f"INSERT OR IGNORE INTO {_table(kind)} "
                 "(session, chat_id, message_id, opened_ts) VALUES (?, ?, ?, ?)",
                 (session, chat_id, message_id, opened_ts))
    conn.commit()


def close_case(conn, kind, session, chat_id, message_id, closed_ts):
    if message_id is None:
        return
    conn.execute(f"UPDATE {_table(kind)} SET closed_ts=max(opened_ts, ?) "
                 "WHERE session=? AND chat_id=? AND message_id=? AND closed_ts IS NULL",
                 (closed_ts, session, chat_id, message_id))
    conn.commit()


def withdraw_case(conn, kind, session, chat_id, message_id, now_ts, sla_seconds):
    """The case message was deleted from the chat (by the lead OR the operator).

    Not yet overdue: a withdrawn request is not a case at all. Already overdue:
    the breach must stay visible, so the case is closed at the deletion moment
    instead of vanishing from the statistics.
    """
    if message_id is None:
        return
    table = _table(kind)
    conn.execute(f"UPDATE {table} SET closed_ts=max(opened_ts, ?), withdrawn=1 WHERE session=? "
                 "AND chat_id=? AND message_id=? AND closed_ts IS NULL AND opened_ts + ? <= ?",
                 (now_ts, session, chat_id, message_id, sla_seconds, now_ts))
    conn.execute(f"DELETE FROM {table} WHERE session=? AND chat_id=? "
                 "AND message_id=? AND closed_ts IS NULL", (session, chat_id, message_id))
    conn.commit()


def period_bounds(period, offset, now=None, custom=None):
    """Return human label, [start, end) in UTC epoch seconds. End capped at now."""
    tz = local_timezone(offset)
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    today = local.date()
    if custom is not None:
        first, last = custom
        if first > last or (last - first).days > 365:
            raise ValueError('Период: от 1 до 366 дней, дата начала не позже даты конца.')
        start, end = first, last + timedelta(days=1)
        label = f'{first:%d.%m.%Y} — {last:%d.%m.%Y}'
    elif period == 'today':
        start, end, label = today, today + timedelta(days=1), 'сегодня'
    elif period == 'yesterday':
        start, end, label = today - timedelta(days=1), today, 'вчера'
    elif period == 'week':
        start, end, label = today - timedelta(days=6), today + timedelta(days=1), '7 дней'
    elif period == 'month':
        start, end, label = today - timedelta(days=29), today + timedelta(days=1), '30 дней'
    else:
        raise ValueError('Неизвестный период')
    utc_start = int(datetime.combine(start, time.min, tzinfo=tz).timestamp())
    utc_end = int(datetime.combine(end, time.min, tzinfo=tz).timestamp())
    return label, utc_start, min(utc_end, int(local.timestamp())) if period != 'yesterday' and start <= today else utc_end


def parse_custom_dates(parts):
    if len(parts) != 2:
        raise ValueError('Формат: /stats 01.09.2026 25.09.2026')
    try:
        return tuple(datetime.strptime(s, '%d.%m.%Y').date() for s in parts)
    except ValueError as error:
        raise ValueError('Дата в формате ДД.ММ.ГГГГ. Например: /stats 01.09.2026 25.09.2026') from error


def work_seconds(first_ts, end_ts, quiet, offset):
    """Elapsed seconds excluding quiet hours in the configured fixed timezone."""
    total = max(0, end_ts - first_ts)
    if quiet is None or not total:
        return total
    tz = local_timezone(offset)
    start_date = datetime.fromtimestamp(first_ts, tz).date() - timedelta(days=1)
    last_date = datetime.fromtimestamp(end_ts, tz).date()
    day = start_date
    while day <= last_date:
        beginning = datetime.combine(day, time.min, tzinfo=tz)
        q_start = int((beginning + timedelta(minutes=quiet.start_minute)).timestamp())
        q_end = int((beginning + timedelta(minutes=quiet.end_minute +
                                          (1440 if quiet.start_minute > quiet.end_minute else 0))).timestamp())
        total -= max(0, min(end_ts, q_end) - max(first_ts, q_start))
        day += timedelta(days=1)
    return max(0, total)


def _summary(conn, table, names, first, end, now_ts, sla, quiet, offset):
    # Bounded by the period for all historical summaries; only live cases are
    # fetched separately. A one-hour breach is counted ONCE per case.
    conditions = ' AND session IN (' + ','.join('?' * len(names)) + ')'
    if not names:
        return dict(new=0, clients=0, handled=0, breaches=0, on_time=0,
                    avg=None, median=None, p90=None, avg_work=None)
    opened = conn.execute(f'SELECT session, chat_id FROM {table} WHERE opened_ts >= ? '
                          f'AND opened_ts < ?{conditions}', (first, end, *names)).fetchall()
    finished = conn.execute(f'SELECT opened_ts, closed_ts FROM {table} WHERE closed_ts >= ? '
                            f'AND closed_ts < ? AND withdrawn = 0{conditions}',
                            (first, end, *names)).fetchall()
    breach = conn.execute(f'SELECT count(*) FROM {table} WHERE opened_ts + ? >= ? AND '
                          f'opened_ts + ? < ? AND opened_ts + ? <= ? AND '
                          f'(closed_ts IS NULL OR closed_ts > opened_ts + ?){conditions}',
                          (sla, first, sla, end, sla, now_ts, sla, *names)).fetchone()[0]
    durations = sorted(max(0, stop - start) for start, stop in finished)
    work = [work_seconds(start, stop, quiet, offset) for start, stop in finished]
    p90 = durations[max(0, (9 * len(durations) + 9) // 10 - 1)] if durations else None
    return dict(new=len(opened), clients=len({chat_id for _session, chat_id in opened}), handled=len(finished),
                breaches=breach, on_time=sum(duration <= sla for duration in durations),
                avg=round(mean(durations)) if durations else None,
                median=round(median(durations)) if durations else None, p90=p90,
                avg_work=round(mean(work)) if work else None)


def snapshot(conn, cfg, names, period='today', now=None, custom=None):
    """Period totals and a separate current backlog. No per-operator rankings."""
    label, first, end = period_bounds(period, cfg['utc_offset_hours'], now, custom)
    current = int((now or datetime.now(timezone.utc)).timestamp())
    ans = _summary(conn, 'stats_answers', names, first, end, current, cfg['sla_seconds'],
                   cfg.get('quiet_window'), cfg['utc_offset_hours'])
    read = _summary(conn, 'stats_reads', names, first, end, current, cfg['sla_seconds'],
                    cfg.get('quiet_window'), cfg['utc_offset_hours'])
    live = {'answers': 0, 'reads': 0, 'answers_overdue': 0, 'reads_overdue': 0}
    if names:
        params = ','.join('?' * len(names))
        for kind in ('answers', 'reads'):
            row = conn.execute(f'SELECT count(*), '
                               f'sum(CASE WHEN opened_ts + ? <= ? THEN 1 ELSE 0 END) '
                               f'FROM stats_{kind} WHERE closed_ts IS NULL AND session IN ({params})',
                               (cfg['sla_seconds'], current, *names)).fetchone()
            live[kind] = row[0]
            live[kind + '_overdue'] = row[1] or 0
    return dict(label=label, start=first, end=end, answers=ans, reads=read, live=live)


def overdue_rows(conn, cfg, names, now=None, limit=None):
    if not names:
        return []
    current = int((now or datetime.now(timezone.utc)).timestamp())
    params = ','.join('?' * len(names))
    # A single conversation may occur in both tables; list it once with two flags.
    rows = conn.execute(f"""SELECT session, chat_id,
              min(CASE WHEN kind='answers' THEN opened_ts END),
              min(CASE WHEN kind='reads' THEN opened_ts END)
           FROM (
              SELECT session, chat_id, opened_ts, 'answers' kind FROM stats_answers
               WHERE closed_ts IS NULL AND session IN ({params})
              UNION ALL
              SELECT session, chat_id, opened_ts, 'reads' kind FROM stats_reads
               WHERE closed_ts IS NULL AND session IN ({params})
           ) GROUP BY session, chat_id""", (*names, *names)).fetchall()
    due = []
    for session, chat_id, answer_ts, read_ts in rows:
        flags = [v for v in (answer_ts, read_ts) if v is not None and current - v >= cfg['sla_seconds']]
        if flags:
            due.append(dict(session=session, chat_id=chat_id, answer_ts=answer_ts,
                            read_ts=read_ts, since=min(flags)))
    ordered = sorted(due, key=lambda item: (item['since'], item['session'], item['chat_id']))
    return ordered[:limit] if limit is not None else ordered


def detailed_rows(conn, table, names, first, end):
    if not names:
        return []
    placeholders = ','.join('?' * len(names))
    return conn.execute(f'SELECT session, chat_id, message_id, opened_ts, closed_ts, withdrawn '
                        f'FROM {table} WHERE ((opened_ts >= ? AND opened_ts < ?) '
                        f'OR (closed_ts >= ? AND closed_ts < ?)) AND session IN ({placeholders})',
                        (first, end, first, end, *names)).fetchall()

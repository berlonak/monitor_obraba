# copyright by berlonak
# telegram: @Kilax123
"""On-demand /stats for the supervisor and the developer only. No scheduled reports."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from html import escape
import logging

from .statistics import overdue_rows, parse_custom_dates, period_bounds, snapshot
from .timeutils import humanize_seconds

LOG = logging.getLogger(__name__)
PERIODS = {'today': 'Сегодня', 'yesterday': 'Вчера', 'week': '7 дней', 'month': '30 дней'}


def _keyboard(rows):
    return {'inline_keyboard': [[{'text': label, 'callback_data': value} for label, value in row]
                                for row in rows]}


def period_buttons():
    return [[('Сегодня', 'p:today'), ('Вчера', 'p:yesterday')],
            [('7 дней', 'p:week'), ('30 дней', 'p:month')]]


def number_duration(value):
    return humanize_seconds(value) if value is not None else '—'


def _operators(cfg):
    return [x['name'] for x in cfg['sessions']]


def _name(cfg, session):
    return next((x['operator_label'] for x in cfg['sessions'] if x['name'] == session), session)


def _display_report(cfg, report, heading):
    answers, reads, live = report['answers'], report['reads'], report['live']
    answered_ratio = f"{answers['on_time'] * 100 // answers['handled']}%" if answers['handled'] else '—'
    return '\n'.join([
        f'📊 <b>{escape(heading)}</b>',
        f"Период: {escape(report['label'])} (UTC{cfg['utc_offset_hours']:+d})",
        '',
        '<b>Ответы</b>',
        f"Новых обращений: {answers['new']}",
        f"Новых лидов: {answers['clients']}",
        f"Ответов за период: {answers['handled']}",
        f"В пределах SLA: {answered_ratio}",
        f"Нарушений SLA: {answers['breaches']}",
        f"Среднее время ответа: {number_duration(answers['avg'])}",
        f"Медиана / P90: {number_duration(answers['median'])} / {number_duration(answers['p90'])}",
        f"Среднее без времени сна: {number_duration(answers['avg_work'])}",
        '',
        '<b>Прочтение</b>',
        f"Случаев с непрочитанными: {reads['new']}",
        f"Прочитано: {reads['handled']}",
        f"Просрочили прочтение: {reads['breaches']}",
        f"Среднее время до прочтения: {number_duration(reads['avg'])}",
        '',
        '<b>Сейчас (по последней проверке)</b>',
        f"Без ответа: {live['answers']} (просрочено: {live['answers_overdue']})",
        f"Непрочитано: {live['reads']} (просрочено: {live['reads_overdue']})",
        '',
        'Время прочтения приблизительное: ориентир — момент проверки.',
        'Без ответа и непрочитанное могут относиться к одному лиду.',
    ])


def summary_view(cfg, state, period='today', selected=None, custom=None, now=None):
    names = [_operators(cfg)[selected]] if selected is not None else _operators(cfg)
    heading = f"Оператор: {_name(cfg, names[0])}" if selected is not None else 'Все операторы'
    report = snapshot(state.conn, cfg, names, period=period, now=now, custom=custom)
    text = _display_report(cfg, report, heading)
    rows = period_buttons()
    if selected is None:
        indices = range(len(cfg['sessions']))
        rows += [[(str(cfg['sessions'][i]['operator_label'])[:36], f'o:{period}:{i}')]
                 for i in indices]
    else:
        rows.append([('Все операторы', f'p:{period}')])
    rows.append([('Просроченные', f'l:{selected if selected is not None else "a"}:0'),
                 ('Excel', f'x:{period}:{selected if selected is not None else "a"}')])
    # Period buttons navigate to aggregate overview by default, on an operator
    # screen the single-operator selector is included in the callback.
    if selected is not None:
        rows[:2] = [[(name, 'o:' + key.split(':')[1] + ':' + str(selected)) for name, key in row]
                    for row in period_buttons()]
    if custom is not None:
        # Custom range doesn't fit into 64-byte callback_data; keep the visible
        # report and use standard preset buttons for navigation / Excel.
        operator_part = names[0] + ' ' if selected is not None else ''
        text += '\n\nExcel за этот период: /stats_excel ' + operator_part + 'ДД.ММ.ГГГГ ДД.ММ.ГГГГ'
        rows[-1] = [('Просроченные', f'l:{selected if selected is not None else "a"}:0')]
    else:
        text += '\n\nСвой период: /stats 01.09.2026 25.09.2026'
    return text, _keyboard(rows)


def overdue_view(cfg, state, selected=None, page=0, now=None):
    names = [_operators(cfg)[selected]] if selected is not None else _operators(cfg)
    due = overdue_rows(state.conn, cfg, names, now=now)
    local_now = int((now or datetime.now(timezone.utc)).timestamp())
    first = page * 12
    page_rows = due[first:first + 12]
    title = _name(cfg, names[0]) if selected is not None else 'Все операторы'
    lines = [f'⚠️ <b>Просроченные • {escape(title)}</b>', f'Всего: {len(due)}', '']
    for item in page_rows:
        overdue_types = []
        if item['answer_ts'] is not None and local_now - item['answer_ts'] >= cfg['sla_seconds']:
            overdue_types.append('без ответа: ' + humanize_seconds(local_now - item['answer_ts']))
        if item['read_ts'] is not None and local_now - item['read_ts'] >= cfg['sla_seconds']:
            overdue_types.append('непрочитано: ' + humanize_seconds(local_now - item['read_ts']))
        lines += [f"<b>{escape(_name(cfg, item['session']))}</b> · "
                  f'<a href="tg://user?id={int(item["chat_id"])}">лид {item["chat_id"]}</a>',
                  ' · '.join(overdue_types), '']
    if not page_rows:
        lines.append('Просроченных обращений нет.' if not due else 'Больше записей нет.')
    who = str(selected) if selected is not None else 'a'
    rows = []
    nav = []
    if page:
        nav.append(('◀ Назад', f'l:{who}:{page-1}'))
    if first + 12 < len(due):
        nav.append(('Далее ▶', f'l:{who}:{page+1}'))
    if nav:
        rows.append(nav)
    rows.append([('К статистике', 'o:today:' + who if who != 'a' else 'p:today')])
    return '\n'.join(lines), _keyboard(rows)


class StatsBot:
    def __init__(self, cfg, state, notifier, tech=None):
        self.cfg = cfg
        self.state = state
        self.notifier = notifier
        self.tech = tech  # technical problems go to the developer only
        self.offset = 0

    def allowed(self, sender, chat):
        """Supervisor (main_telegram_id) and developer (developer_telegram_id),
        each only in their own private chat with the bot. Operators: never."""
        uid = sender.get('id')
        viewers = {self.cfg.get('main_telegram_id'), self.cfg.get('developer_telegram_id')} - {None}
        return uid in viewers and chat.get('type') == 'private' and chat.get('id') == uid

    async def _send_view(self, chat_id, text, keyboard=None, edit=None):
        args = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML',
                'disable_web_page_preview': True}
        if keyboard:
            args['reply_markup'] = keyboard
        if edit is not None:
            args['message_id'] = edit
        result = await self.notifier.request('editMessageText' if edit is not None else 'sendMessage', args)
        if not result.get('ok') and 'message is not modified' not in result.get('description', ''):
            LOG.warning('Не удалось показать статистику: %s', result.get('description', 'Bot API error'))

    async def _file(self, chat_id, period, selected=None, custom=None):
        from .stats_excel import make_excel
        names = [_operators(self.cfg)[selected]] if selected is not None else _operators(self.cfg)
        filename, payload = make_excel(self.cfg, self.state, names, period, custom)
        if not await self.notifier.send_document(chat_id, filename, payload):
            await self.notifier.send(chat_id, 'Не получилось отправить файл. Попробуй ещё раз.')

    async def on_message(self, msg):
        chat, sender = msg.get('chat', {}), msg.get('from', {})
        if not self.allowed(sender, chat):
            return  # never show statistics in group chats or to operators
        command = (msg.get('text') or '').strip().split()
        if not command:
            return
        action = command[0].split('@')[0].lower()
        if action not in ('/stats', '/stats_excel'):
            return
        try:
            parts = command[1:]
            selected = None
            if parts and parts[0] in _operators(self.cfg):
                selected = _operators(self.cfg).index(parts.pop(0))
            custom = parse_custom_dates(parts) if parts else None
            if custom:
                period_bounds('today', self.cfg['utc_offset_hours'], custom=custom)
            if action == '/stats_excel':
                await self._file(chat['id'], 'today', selected, custom)
            else:
                message, keyboard = summary_view(self.cfg, self.state, custom=custom, selected=selected)
                await self._send_view(chat['id'], message, keyboard)
        except ValueError as exc:
            await self.notifier.send(chat['id'], str(exc))

    async def on_callback(self, callback):
        sender = callback.get('from', {})
        msg = callback.get('message', {})
        chat = msg.get('chat', {})
        # A valid callback ID is required even for unauthorized users to stop
        # their client-side loading spinner. No statistical information leaks.
        if not self.allowed(sender, chat):
            await self.notifier.request('answerCallbackQuery', {
                'callback_query_id': callback['id'], 'text': 'Нет доступа'})
            return
        payload = (callback.get('data') or '').split(':')
        try:
            mode = payload[0]
            if mode == 'p' and len(payload) == 2 and payload[1] in PERIODS:
                text, kb = summary_view(self.cfg, self.state, payload[1])
            elif mode == 'o' and len(payload) == 3 and payload[1] in PERIODS:
                idx = int(payload[2])
                if idx < 0 or idx >= len(self.cfg['sessions']):
                    raise ValueError('Неверный оператор')
                text, kb = summary_view(self.cfg, self.state, payload[1], idx)
            elif mode == 'l' and len(payload) == 3:
                idx = None if payload[1] == 'a' else int(payload[1])
                page = int(payload[2])
                if (idx is not None and not 0 <= idx < len(self.cfg['sessions'])) or not 0 <= page < 500:
                    raise ValueError('Неверная страница')
                text, kb = overdue_view(self.cfg, self.state, idx, page)
            elif mode == 'x' and len(payload) == 3 and payload[1] in PERIODS:
                idx = None if payload[2] == 'a' else int(payload[2])
                if idx is not None and not 0 <= idx < len(self.cfg['sessions']):
                    raise ValueError('Неверный оператор')
                await self.notifier.request('answerCallbackQuery', {'callback_query_id': callback['id']})
                await self._file(chat['id'], payload[1], idx)
                return
            else:
                raise ValueError('Кнопка устарела. Отправь /stats ещё раз.')
            await self.notifier.request('answerCallbackQuery', {'callback_query_id': callback['id']})
            await self._send_view(chat['id'], text, kb, msg.get('message_id'))
        except (TypeError, ValueError) as exc:
            await self.notifier.request('answerCallbackQuery', {
                'callback_query_id': callback['id'], 'text': str(exc)[:180], 'show_alert': True})

    async def handle(self, update):
        if 'message' in update:
            await self.on_message(update['message'])
        elif 'callback_query' in update:
            await self.on_callback(update['callback_query'])

    async def poll(self):
        # Ignore pre-start messages. The separate whoami command must be run
        # while 'run' is stopped; Telegram supports only one polling consumer.
        try:
            response = await self.notifier.request('deleteWebhook', {'drop_pending_updates': False})
            if not response.get('ok'):
                LOG.warning('deleteWebhook: %s', response.get('description'))
        except Exception:
            LOG.warning('Не удалось проверить webhook бота; продолжаю опрос')
        # Resume past queued updates rather than dropping commands received
        # while a monitor was temporarily down. The owner is checked per update.
        while True:
            try:
                response = await self.notifier.request('getUpdates', {
                    'offset': self.offset, 'timeout': 25,
                    'allowed_updates': ['message', 'callback_query'],
                })
                if not response.get('ok'):
                    delay = response.get('parameters', {}).get('retry_after', 5)
                    LOG.warning('Bot API getUpdates: %s', response.get('description', 'ошибка'))
                    if response.get('error_code') == 409 and self.tech is not None:
                        await self.tech.problem(
                            'bot:getupdates',
                            '🟠 Бот: Telegram вернул 409 Conflict на getUpdates — этим же ботом '
                            'пользуется другой процесс (второй монитор, whoami на другом компьютере '
                            'или webhook). Команды /stats могут не доходить.')
                    await asyncio.sleep(max(2, min(60, int(delay))))
                    continue
                if self.tech is not None:
                    await self.tech.resolved('bot:getupdates',
                                             '🟢 Бот: конфликт getUpdates закончился, /stats работает.')
                for update in response.get('result', []):
                    self.offset = max(self.offset, update['update_id'] + 1)
                    try:
                        await self.handle(update)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        LOG.exception('Ошибка команды статистики (монитор продолжает работать)')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Avoid exception text containing the Bot API URL and token.
                LOG.warning('Нет соединения с ботом (%s), повтор через 5 сек.', type(exc).__name__)
                await asyncio.sleep(5)

# copyright by berlonak
# telegram: @Kilax123
"""Optional rotating diagnostic log, without Telegram API secrets or message bodies."""
import logging
from logging.handlers import RotatingFileHandler
import re
from pathlib import Path

_BOT_URL = re.compile(r'(https?://api\.telegram\.org/bot)[^/\s?]+', re.I)


def redact(cfg, text):
    """Remove the bot token / api_hash from any text leaving the process."""
    text = _BOT_URL.sub(r'\1[TOKEN_HIDDEN]', str(text))
    for key in ('notifier_bot_token', 'api_hash'):
        secret = cfg.get(key)
        if secret:
            text = text.replace(str(secret), '[SECRET_HIDDEN]')
    return text


class SecretFormatter(logging.Formatter):
    """Redact secrets even inside exception tracebacks."""
    def __init__(self, fmt, cfg):
        super().__init__(fmt)
        self.secrets = [str(cfg.get(key, '')) for key in ('notifier_bot_token', 'api_hash')
                        if cfg.get(key)]
        self.bot_url = re.compile(r'(https?://api\.telegram\.org/bot)[^/\s?]+', re.I)

    def format(self, record):
        message = super().format(record)
        message = self.bot_url.sub(r'\1[TOKEN_HIDDEN]', message)
        for secret in self.secrets:
            message = message.replace(secret, '[SECRET_HIDDEN]')
        return message


class SecretFilter(logging.Filter):
    """Keep API credentials out of logs, including third-party exception text."""
    def __init__(self, cfg):
        super().__init__()
        self.secrets = [str(cfg.get(key, '')) for key in ('notifier_bot_token', 'api_hash')
                        if cfg.get(key)]
        self.bot_url = re.compile(r'(https?://api\.telegram\.org/bot)[^/\s?]+', re.I)

    def filter(self, record):
        # Formatting here also sanitizes exceptions carried as text. Avoid
        # modifying shared LogRecord args for other attached handlers.
        try:
            message = record.getMessage()
        except Exception:
            message = '[не удалось сформировать сообщение журнала]'
        message = self.bot_url.sub(r'\1[TOKEN_HIDDEN]', message)
        for secret in self.secrets:
            message = message.replace(secret, '[SECRET_HIDDEN]')
        record.msg = message
        record.args = ()
        return True


def configure_logging(cfg):
    """Keep the console concise; capture per-dialog diagnostics in a separate file."""
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    formatter = SecretFormatter(
        '%(asctime)s %(levelname)-7s [%(name)s:%(lineno)d] %(message)s', cfg)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(SecretFormatter('%(asctime)s %(levelname)s %(message)s', cfg))
    console.addFilter(SecretFilter(cfg))
    root.addHandler(console)
    root.setLevel(logging.INFO)
    if not cfg.get('debug_logging', False):
        return
    target = Path(cfg['debug_log_file'])
    target.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(target, maxBytes=10 * 1024 * 1024,
                                       backupCount=3, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(SecretFilter(cfg))
    root.addHandler(file_handler)
    root.setLevel(logging.DEBUG)
    # HTTP libraries can expose full Bot API URLs when verbose. Their INFO+
    # errors still go to both handlers through root after token sanitization.
    logging.getLogger('aiohttp').setLevel(logging.INFO)
    logging.getLogger('asyncio').setLevel(logging.INFO)
    logging.getLogger('telethon').setLevel(logging.INFO)
    logging.getLogger(__name__).info('DEBUG включён: подробный журнал %s (ротация 10 МБ × 3)', target)

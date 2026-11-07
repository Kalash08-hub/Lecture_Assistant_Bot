"""Application settings. Loading is harmless; validation happens before polling."""
import base64
import binascii
import os
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / '.env')


def project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def number(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        raise ValueError(f'{name}: ожидается целое число') from None


def boolean(name: str, default: bool = True) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw not in {'true', 'false'}:
        raise ValueError(f'{name}: используйте true или false')
    return raw == 'true'


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(default='', repr=False)
    credentials: str = field(default='', repr=False)
    proxy_url: str = field(default='', repr=False)
    admin_id: int = 0
    model: str = 'GigaChat-2'
    scope: str = 'GIGACHAT_API_PERS'
    ca_file: Path | None = None
    upload_dir: Path = PROJECT_ROOT / 'files'
    database_path: Path = PROJECT_ROOT / 'data/bot.sqlite3'
    max_upload_bytes: int = 20 * 1024 * 1024
    max_user_files: int = 20
    max_user_bytes: int = 100 * 1024 * 1024
    retention_days: int = 30
    queue_size: int = 10
    workers: int = 1
    daily_limit: int = 10
    queue_timeout: int = 300
    execution_timeout: int = 240
    upload_timeout: int = 90
    http_timeout: int = 90
    cleanup_interval: int = 3600
    orphan_grace: int = 3600
    support_cooldown: int = 300
    support_enabled: bool = True
    ai_enabled: bool = True
    log_level: str = 'INFO'

    @classmethod
    def from_env(cls) -> 'Settings':
        return cls(
            bot_token=os.getenv('BOT_TOKEN', '').strip(),
            credentials=os.getenv('GIGACHAT_CREDENTIALS', '').strip(),
            proxy_url=os.getenv('PROXY_URL', '').strip(),
            admin_id=number('ADMIN_ID', 0), model=os.getenv('GIGACHAT_MODEL') or 'GigaChat-2',
            scope=os.getenv('GIGACHAT_SCOPE') or 'GIGACHAT_API_PERS',
            ca_file=project_path(os.environ['GIGACHAT_CA_BUNDLE_FILE']) if os.getenv('GIGACHAT_CA_BUNDLE_FILE') else None,
            upload_dir=project_path(os.getenv('UPLOAD_DIR') or 'files'),
            database_path=project_path(os.getenv('DATABASE_PATH') or 'data/bot.sqlite3'),
            **{key: number(env, default) for key, env, default in (
                ('max_upload_bytes', 'MAX_UPLOAD_BYTES', 20 * 1024 * 1024),
                ('max_user_files', 'MAX_USER_FILES', 20), ('max_user_bytes', 'MAX_USER_BYTES', 100 * 1024 * 1024),
                ('retention_days', 'RETENTION_DAYS', 30), ('queue_size', 'QUEUE_SIZE', 10),
                ('workers', 'GENERATION_WORKERS', 1), ('daily_limit', 'DAILY_GENERATION_LIMIT', 10),
                ('queue_timeout', 'QUEUE_TIMEOUT_SECONDS', 300), ('execution_timeout', 'GENERATION_TIMEOUT_SECONDS', 240),
                ('upload_timeout', 'UPLOAD_TIMEOUT_SECONDS', 90), ('http_timeout', 'HTTP_TIMEOUT_SECONDS', 90),
                ('cleanup_interval', 'CLEANUP_INTERVAL_SECONDS', 3600), ('orphan_grace', 'ORPHAN_GRACE_SECONDS', 3600),
                ('support_cooldown', 'SUPPORT_COOLDOWN_SECONDS', 300),
            )},
            ai_enabled=boolean('AI_ENABLED'),
            support_enabled=boolean('SUPPORT_ENABLED'),
            log_level=os.getenv('LOG_LEVEL') or 'INFO',
        )

    def ssl_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if self.ca_file:
            context.load_verify_locations(str(self.ca_file))
        return context

    def validate(self) -> None:
        from aiogram.utils.token import validate_token
        try:
            validate_token(self.bot_token)
        except Exception:
            raise ValueError('BOT_TOKEN: задайте корректный токен Telegram-бота') from None
        if self.ai_enabled and not self.credentials:
            raise ValueError('GIGACHAT_CREDENTIALS: задайте ключ либо AI_ENABLED=false')
        if self.ai_enabled and (self.credentials.lower().startswith('basic ') or any(c.isspace() for c in self.credentials)):
            raise ValueError('GIGACHAT_CREDENTIALS: укажите ключ без Basic и пробелов')
        if self.ai_enabled:
            try:
                pair = base64.b64decode(self.credentials, validate=True).decode('utf-8')
                left, right = pair.split(':', 1)
                if not left or not right:
                    raise ValueError
            except (binascii.Error, UnicodeError, ValueError):
                raise ValueError('GIGACHAT_CREDENTIALS: нужен ключ авторизации Base64, а не Client ID или access token') from None
        if self.ai_enabled and self.scope not in {'GIGACHAT_API_PERS', 'GIGACHAT_API_B2B', 'GIGACHAT_API_CORP'}:
            raise ValueError('GIGACHAT_SCOPE: неизвестная область доступа')
        if not self.model.strip():
            raise ValueError('GIGACHAT_MODEL: значение не должно быть пустым')
        if self.proxy_url:
            try:
                parsed = urlsplit(self.proxy_url)
                valid = parsed.scheme in {'http', 'socks4', 'socks5'} and parsed.hostname and parsed.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('PROXY_URL: ожидается http/socks4/socks5 URL с адресом и портом')
        for key in ('max_upload_bytes', 'max_user_files', 'max_user_bytes', 'retention_days', 'queue_size',
                    'workers', 'daily_limit', 'queue_timeout', 'execution_timeout', 'upload_timeout',
                    'http_timeout', 'cleanup_interval', 'orphan_grace', 'support_cooldown'):
            if getattr(self, key) < 1:
                raise ValueError(f'{key}: значение должно быть больше нуля')
        if self.max_upload_bytes > 20 * 1024 * 1024:
            raise ValueError('MAX_UPLOAD_BYTES: максимум 20971520 для облачного Telegram Bot API')
        if self.log_level not in {'DEBUG', 'INFO', 'WARNING', 'ERROR'}:
            raise ValueError('LOG_LEVEL: используйте DEBUG, INFO, WARNING или ERROR')
        try:
            if self.ai_enabled:
                self.ssl_context()
        except (OSError, ssl.SSLError):
            raise ValueError('GIGACHAT_CA_BUNDLE_FILE: не удалось загрузить доверенный PEM') from None
        try:
            images = {p.name for p in (PROJECT_ROOT / 'assets').iterdir()}
        except OSError:
            raise ValueError('Каталог assets недоступен') from None
        for filename in ('bot_avatar.png', 'Telling_bot.png', 'waiting.png', 'accepted.png', 'help.png'):
            if filename not in images:
                raise ValueError(f'Отсутствует assets/{filename} (проверьте регистр)')

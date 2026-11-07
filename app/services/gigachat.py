"""Reusable GigaChat HTTP/OAuth client. No request bodies or secrets in logs."""
import asyncio
import logging
import time
from uuid import UUID, uuid4

import aiohttp

from app.config import Settings
from app.database.store import Store

log = logging.getLogger(__name__)
AUTH_URL = 'https://ngw.devices.sberbank.ru:9443/api/v2/oauth'
API_URL = 'https://api.giga.chat/v1'


class StudyAIError(Exception):
    def __init__(self, message: str, category: str = 'api'):
        self.category = category
        super().__init__(message)


def http_error(status: int, stage: str) -> StudyAIError:
    if status in {401, 403}:
        return StudyAIError('Нет доступа к GigaChat. Проверьте ключ и область доступа.', 'access')
    if status == 402:
        return StudyAIError('Исчерпан баланс GigaChat. Обратитесь к администратору бота.', 'balance')
    if status == 429:
        return StudyAIError('GigaChat ограничил частоту запросов. Повторите позже.', 'rate_limit')
    if status >= 500:
        return StudyAIError('GigaChat временно недоступен. Повторите позже.', 'unavailable')
    if stage == 'загрузка файла' and status in {400, 415}:
        return StudyAIError(f'GigaChat не смог прочитать файл (HTTP {status}). Пересохраните документ в поддерживаемом формате.', 'document')
    if status in {413, 422}:
        return StudyAIError('Файл слишком большой или превышает контекст модели. Разделите лекцию.', 'document')
    return StudyAIError(f'GigaChat отклонил запрос: {stage}, HTTP {status}. Проверьте документ и доступность модели.', 'request')


class GigaChatClient:
    def __init__(self, settings: Settings, store: Store | None = None, session=None):
        self.settings = settings
        self.store = store
        self.session = session
        self.owns_session = session is None
        self.token: str | None = None
        self.expires = 0.0
        self.token_lock = asyncio.Lock()
        self.cleanup_tasks: set[asyncio.Task] = set()

    async def open(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=self.settings.ssl_context()),
                timeout=aiohttp.ClientTimeout(total=self.settings.http_timeout, connect=20),
                trust_env=False,
            )

    async def close(self) -> None:
        if self.cleanup_tasks:
            done, pending = await asyncio.wait(self.cleanup_tasks, timeout=6)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if self.owns_session and self.session:
            await self.session.close()
        self.session = None

    async def request_json(self, method: str, url: str, *, stage: str, retry_safe: bool = False, **kwargs) -> dict:
        for attempt in range(3 if retry_safe else 1):
            try:
                async with self.session.request(method, url, **kwargs) as response:
                    status = response.status
                    if status == 401 and url != AUTH_URL:
                        self.token, self.expires = None, 0
                    if status >= 400:
                        if retry_safe and attempt < 2 and (status == 429 or status >= 500):
                            log.warning('ai_http_retry stage=%s status=%s attempt=%s', stage, status, attempt + 1)
                            await asyncio.sleep(0.25 * 2**attempt)
                            continue
                        raise http_error(status, stage)
                    try:
                        data = await response.json()
                    except (ValueError, aiohttp.ContentTypeError):
                        raise StudyAIError('GigaChat вернул некорректный ответ. Повторите позже.', 'response') from None
                    if not isinstance(data, dict):
                        raise StudyAIError('GigaChat вернул некорректный ответ.', 'response')
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                if retry_safe and attempt < 2:
                    await asyncio.sleep(0.25 * 2**attempt)
                    continue
                category = 'timeout' if isinstance(error, asyncio.TimeoutError) else 'network'
                log.warning('ai_connection stage=%s category=%s type=%s', stage, category, type(error).__name__)
                raise StudyAIError('Не удалось связаться с GigaChat. Запрос генерации автоматически не повторяется; проверьте сеть и CA.', category) from None
        raise StudyAIError('GigaChat недоступен.', 'unavailable')

    async def access_token(self) -> str:
        async with self.token_lock:
            if self.token and self.expires - time.time() > 60:
                return self.token
            if not self.settings.credentials:
                raise StudyAIError('Генерация временно недоступна: GigaChat не настроен.', 'configuration')
            data = await self.request_json(
                'POST', AUTH_URL, stage='авторизация', retry_safe=True,
                headers={'Authorization': 'Basic ' + self.settings.credentials, 'RqUID': str(uuid4())},
                data={'scope': self.settings.scope})
            try:
                token = data['access_token']
                expiry = float(data['expires_at'])
                if expiry > 100_000_000_000:
                    expiry /= 1000
                if not isinstance(token, str) or not token or expiry <= time.time():
                    raise ValueError
                self.token, self.expires = token, min(expiry, time.time() + 1800)
            except (KeyError, TypeError, ValueError):
                raise StudyAIError('Некорректный ответ авторизации GigaChat.', 'response') from None
            log.info('ai_oauth_refreshed')
            return self.token

    async def upload(self, path, mime: str) -> str:
        headers = {'Authorization': 'Bearer ' + await self.access_token()}
        with path.open('rb') as stream:
            form = aiohttp.FormData()
            form.add_field('purpose', 'general')
            form.add_field('file', stream, filename=path.name, content_type=mime)
            data = await self.request_json('POST', API_URL + '/files', stage='загрузка файла', headers=headers, data=form)
        file_id = data.get('id')
        try:
            if not isinstance(file_id, str):
                raise ValueError
            file_id = str(UUID(file_id))
        except (ValueError, TypeError):
            raise StudyAIError('GigaChat не вернул корректный ID вложения.', 'response') from None
        if self.store:
            await self.store.schedule_remote_cleanup(file_id)
        return file_id

    async def complete(self, payload: dict) -> dict:
        return await self.request_json('POST', API_URL + '/chat/completions', stage='генерация',
                                       headers={'Authorization': 'Bearer ' + await self.access_token()}, json=payload)

    def clean_later(self, file_id: str) -> None:
        task = asyncio.create_task(self.cleanup_file(file_id))
        self.cleanup_tasks.add(task)
        def completed(finished):
            self.cleanup_tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                log.warning('remote_cleanup_task_failed type=%s', type(finished.exception()).__name__)
        task.add_done_callback(completed)

    async def cleanup_file(self, file_id: str) -> None:
        success = False
        try:
            async with asyncio.timeout(5):
                token = await self.access_token()
                async with self.session.request('POST', API_URL + '/files/' + file_id + '/delete',
                                                headers={'Authorization': 'Bearer ' + token}) as response:
                    success = 200 <= response.status < 300 or response.status == 404
                    await response.read()
                    if not success:
                        log.warning('remote_cleanup_failed status=%s', response.status)
        except (StudyAIError, aiohttp.ClientError, asyncio.TimeoutError):
            log.warning('remote_cleanup_failed category=network')
        finally:
            if self.store:
                await self.store.remote_cleanup_done(file_id, success)

    async def retry_cleanup(self) -> None:
        if self.store:
            for item in await self.store.pending_cleanup():
                await self.cleanup_file(item['file_id'])

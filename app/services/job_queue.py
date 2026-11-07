"""Bounded, cancellable in-process queue; SQLite owns admission and job status."""
import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from app.config import Settings
from app.database.store import AdmissionError, Record, Store
from app.services.prompts import PROMPTS, PROMPT_VERSION
from app.services.study_ai import StudyAIError

log = logging.getLogger(__name__)
EventHandler = Callable[[Record, str, Record | None, str | None], Awaitable[None]]


@dataclass(frozen=True)
class Submission:
    kind: str
    record: Record


class JobQueue:
    def __init__(self, store: Store, ai, settings: Settings, on_event: EventHandler | None = None):
        self.store, self.ai, self.settings = store, ai, settings
        self.on_event = on_event
        self.pending: deque[Record] = deque()
        self.condition = asyncio.Condition()
        self.tasks: list[asyncio.Task] = []
        self.closed = False

    async def start(self) -> None:
        self.tasks = [asyncio.create_task(self._worker(), name=f'study-worker-{i}') for i in range(self.settings.workers)]
        self.tasks.append(asyncio.create_task(self._expire_loop(), name='queue-deadlines'))

    async def submit(self, owner: int, lecture_id: int, mode: str, chat_id: int) -> Submission:
        async with self.condition:
            if self.closed:
                raise AdmissionError('shutdown', 'Бот завершает работу. Повторите позже.')
            if mode not in PROMPTS:
                raise AdmissionError('mode', 'Неизвестное действие.')
            lecture = await self.store.lecture(owner, lecture_id)
            if not lecture:
                raise AdmissionError('missing', 'Лекция удалена или недоступна. Откройте «Мои лекции».')
            cached = await self.store.cached(owner, lecture['sha256'], mode, self.settings.model, PROMPT_VERSION)
            if cached:
                log.info('cache_hit owner=%s lecture=%s mode=%s result=%s', owner, lecture_id, mode, cached['id'])
                return Submission('cached', cached)
            active = await self.store.active_job(owner)
            if active:
                return Submission('active', active)
            if not self.settings.ai_enabled:
                raise AdmissionError('disabled', 'Создание новых материалов временно отключено. Сохранённые результаты доступны в истории.')
            if lecture['expires'] <= time.time() or not Path(lecture['path']).is_file():
                raise AdmissionError('missing', 'Срок хранения файла истёк или файл недоступен. Загрузите лекцию заново.')
            if len(self.pending) >= self.settings.queue_size:
                raise AdmissionError('queue_full', 'Очередь заполнена. Попробуйте позже; лимит генераций не списан.')
            job = await self.store.admit(owner, lecture_id, mode, self.settings.model, PROMPT_VERSION,
                                          chat_id, self.settings.daily_limit)
            self.pending.append(job)
            await self._notify(job, 'queued')
            self.condition.notify()
            log.info('job_queued job=%s owner=%s lecture=%s mode=%s', job['id'], owner, lecture_id, mode)
            return Submission('queued', job)

    async def cancel(self, owner: int, job_id: int | None = None) -> bool:
        async with self.condition:
            for job in list(self.pending):
                if job['owner'] == owner and (job_id is None or job['id'] == job_id):
                    await self.store.set_job_status(job['id'], 'cancelled', 'user_cancelled')
                    self.pending.remove(job)
                    log.info('job_cancelled job=%s owner=%s', job['id'], owner)
                    return True
            return False

    async def _worker(self) -> None:
        while True:
            async with self.condition:
                await self.condition.wait_for(lambda: self.pending or self.closed)
                if self.closed:
                    return
                job = self.pending.popleft()
                if time.time() - job['created'] >= self.settings.queue_timeout:
                    await self.store.set_job_status(job['id'], 'error', 'queue_timeout')
                    expired = True
                else:
                    await self.store.set_job_status(job['id'], 'running')
                    expired = False
            if expired:
                await self._notify(job, 'error', message='Время ожидания в очереди истекло. Можно повторить действие.')
                continue
            await self._execute(job)

    async def _execute(self, job: Record) -> None:
        started = time.monotonic()
        await self._notify(job, 'running')
        try:
            lecture = await self.store.lecture(job['owner'], job['lecture_id'])
            if not lecture:
                raise StudyAIError('Лекция недоступна.', 'document')
            async with asyncio.timeout(self.settings.execution_timeout):
                material = await self.ai.generate(Path(lecture['path']), job['mode'])
            result = await self.store.finish(job, material.content, material.usage)
        except asyncio.CancelledError:
            await self.store.set_job_status(job['id'], 'interrupted', 'shutdown')
            log.info('job_interrupted job=%s', job['id'])
            raise
        except Exception as error:
            if isinstance(error, asyncio.TimeoutError):
                category, message = 'execution_timeout', 'Время создания материала истекло. Автоматического повтора не будет.'
            elif isinstance(error, StudyAIError):
                category, message = error.category, str(error)
            else:
                category, message = 'internal', 'Не удалось создать материал. Повторите позже или обратитесь в поддержку.'
            await self.store.set_job_status(job['id'], 'error', category)
            log.warning('job_error job=%s category=%s duration=%.3f type=%s', job['id'], category, time.monotonic() - started, type(error).__name__)
            await self._notify(job, 'error', message=message)
            return
        log.info('job_done job=%s duration=%.3f usage=%s', job['id'], time.monotonic() - started, material.usage)
        await self._notify(job, 'done', result=result)

    async def _expire_loop(self) -> None:
        while True:
            await asyncio.sleep(min(1, self.settings.queue_timeout / 4))
            expired = []
            async with self.condition:
                for job in list(self.pending):
                    if time.time() - job['created'] >= self.settings.queue_timeout:
                        await self.store.set_job_status(job['id'], 'error', 'queue_timeout')
                        self.pending.remove(job)
                        expired.append(job)
            for job in expired:
                await self._notify(job, 'error', message='Время ожидания в очереди истекло. Можно повторить действие.')

    async def _notify(self, job: Record, status: str, result: Record | None = None, message: str | None = None) -> None:
        if self.on_event:
            try:
                async with asyncio.timeout(10):
                    await self.on_event(job, status, result, message)
            except Exception:
                # Delivery cannot change generation status or trigger another paid request.
                log.warning('job_delivery_failed job=%s status=%s', job['id'], status)

    async def close(self) -> None:
        async with self.condition:
            self.closed = True
            for job in self.pending:
                await self.store.set_job_status(job['id'], 'interrupted', 'shutdown')
            self.pending.clear()
            self.condition.notify_all()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        if self.store.connection:
            await self.store.interrupt_active()

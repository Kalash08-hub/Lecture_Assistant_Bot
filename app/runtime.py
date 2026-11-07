"""Application-owned resources, opened once and closed in reverse order."""
import asyncio
import logging

from app.config import Settings
from app.database.store import Store
from app.keyboards.inline import job_keyboard, keyboard
from app.services.delivery import deliver_result
from app.services.gigachat import GigaChatClient
from app.services.job_queue import JobQueue
from app.services.lecture_storage import LectureFiles
from app.services.study_ai import StudyAI

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings, bot, ai=None):
        self.settings, self.bot = settings, bot
        self.store = Store(settings.database_path)
        self.files = LectureFiles(self.store, settings)
        self.client = GigaChatClient(settings, self.store) if ai is None and settings.ai_enabled else None
        self.ai = ai if ai is not None else (StudyAI(self.client) if self.client else None)
        self.queue = JobQueue(self.store, self.ai, settings, self.job_event)
        self.maintenance: asyncio.Task | None = None
        self.updates: set[asyncio.Task] = set()
        self.closing = False

    async def open(self, workers: bool = True) -> None:
        await self.store.open()
        if self.client:
            await self.client.open()
        await self.files.cleanup()
        if workers:
            await self.queue.start()
        self.maintenance = asyncio.create_task(self._maintain(), name='maintenance')

    async def close(self) -> None:
        self.closing = True
        updates = [task for task in self.updates if task is not asyncio.current_task()]
        for task in updates:
            task.cancel()
        await asyncio.gather(*updates, return_exceptions=True)
        if self.maintenance:
            self.maintenance.cancel()
            await asyncio.gather(self.maintenance, return_exceptions=True)
        await self.queue.close()
        if self.client:
            await self.client.close()
        await self.store.close()

    async def _maintain(self) -> None:
        while True:
            await asyncio.sleep(self.settings.cleanup_interval)
            try:
                await self.files.cleanup()
                if self.client:
                    await self.client.retry_cleanup()
            except Exception as error:
                log.warning('maintenance_error type=%s', type(error).__name__)

    async def job_event(self, job, status, result=None, message=None) -> None:
        lecture = await self.store.lecture(job['owner'], job['lecture_id'])
        name = f"{lecture['name']} (лекция №{lecture['id']})" if lecture else 'Лекция'
        if status == 'done':
            await deliver_result(self.bot, job['chat_id'], result, name)
            return
        statuses = {'queued': 'В очереди', 'running': 'Создаю материал', 'error': 'Ошибка'}
        text = f"{statuses[status]} · Задание №{job['id']}\nФайл: {name[:250]}"
        if message:
            text += '\n' + message
        markup = job_keyboard(job['id']) if status in {'queued', 'running'} else keyboard([[('Открыть лекцию', f"lec:{job['lecture_id']}")]])
        await self.bot.send_message(job['chat_id'], text, reply_markup=markup, parse_mode=None)

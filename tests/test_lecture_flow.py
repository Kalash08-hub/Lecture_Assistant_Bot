import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.handlers.document import receive_pdf
from app.handlers.study import study_callback
from app.runtime import Runtime
from app.services.delivery import utf16_length
from app.services.gigachat import StudyAIError
from app.states.upload import UploadState
from helpers import FakeAI, job_done, test_settings, wait_until, write_fixture


class LectureFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=20, user_id=20))
        self.counter = 100
        self.msg = NS(document=NS(file_name='lecture.txt', file_id='file', file_size=None), from_user=NS(id=20), chat=NS(id=20),
                      answer=AsyncMock(), answer_photo=AsyncMock(side_effect=self.photo))
        self.bot = NS(get_file=AsyncMock(return_value=NS(file_path='remote')), download_file=AsyncMock(side_effect=self.download),
                      send_message=AsyncMock(), send_document=AsyncMock())
        self.ai = FakeAI()
        self.runtime = Runtime(test_settings(self.root), self.bot, self.ai)
        await self.runtime.open()

    async def asyncTearDown(self):
        await self.runtime.close(); await self.storage.close(); self.tmp.cleanup()

    async def photo(self, **kwargs):
        self.counter += 1
        return NS(message_id=self.counter)

    async def download(self, source, destination):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / self.msg.document.file_name
            write_fixture(path)
            destination.write(path.read_bytes())

    async def upload(self):
        await self.state.set_state(UploadState.waiting_for_file)
        await receive_pdf(self.msg, self.bot, self.state, self.runtime)
        rows, _ = await self.runtime.store.lectures(20)
        return rows[0] if rows else None

    def callback(self, mid, action='/abs', owner=20):
        return NS(data=action, from_user=NS(id=owner), answer=AsyncMock(), message=NS(message_id=mid, chat=NS(id=20), answer=AsyncMock()))

    async def generate(self, lecture, mode='outline'):
        submission = await self.runtime.queue.submit(20, lecture['id'], mode, 20)
        if submission.kind == 'queued':
            await wait_until(lambda: job_done(self.runtime.store, 20, submission.record['id']))
        return submission

    async def test_same_name_old_buttons_and_both_modes(self):
        first = await self.upload(); first_message = self.counter
        second = await self.upload()
        self.assertNotEqual(first['path'], second['path'])
        self.assertTrue(Path(first['path']).exists())  # History now retains previous lectures intentionally.
        self.assertIsNone(await self.state.get_state())
        await study_callback(self.callback(first_message), self.runtime)
        active = await self.runtime.store.active_job(20)
        if active:
            await wait_until(lambda: job_done(self.runtime.store, 20, active['id']))
        self.assertEqual(self.ai.calls[0], (first['path'], 'outline'))
        await self.generate(second, 'outline'); await self.generate(second, 'short')
        # Same content/name can use the owner's cache, but modes remain separate.
        self.assertEqual([mode for _, mode in self.ai.calls], ['outline', 'short'])
        self.assertTrue(Path(second['path']).exists())

    async def test_failed_upload_keeps_previous(self):
        first = await self.upload()
        self.bot.download_file.side_effect = OSError('disk full')
        await self.upload()
        rows, count = await self.runtime.store.lectures(20)
        self.assertEqual(count, 1); self.assertEqual(rows[0]['path'], first['path'])
        self.assertEqual(len(list((self.runtime.settings.upload_dir / '20').iterdir())), 1)

    async def test_replacement_during_generation(self):
        first = await self.upload(); self.ai.gate = asyncio.Event()
        submitted = await self.runtime.queue.submit(20, first['id'], 'outline', 20)
        await self.ai.started.wait(); second = await self.upload()
        self.assertTrue(Path(first['path']).exists())
        from app.database.store import AdmissionError
        with self.assertRaises(AdmissionError):
            await self.runtime.files.delete(20, first['id'])
        self.ai.gate.set(); await wait_until(lambda: job_done(self.runtime.store, 20, submitted.record['id']))
        self.assertTrue(Path(first['path']).exists()); self.assertTrue(Path(second['path']).exists())

    async def test_restart_documents_and_long_response(self):
        await study_callback(self.callback(1), self.runtime)
        self.assertEqual(self.ai.calls, [])
        self.msg.document.file_name = 'lecture.docx'; doc = await self.upload()
        self.ai.text = 'Я😀' * 2500
        await self.generate(doc)
        await asyncio.sleep(.01)
        self.bot.send_document.assert_awaited_once()
        export = self.bot.send_document.await_args.args[1]
        self.assertTrue(export.filename.endswith('.docx'))
        self.assertTrue(all(utf16_length(c.args[1]) <= 4096 for c in self.bot.send_message.await_args_list))

    async def test_ai_error_keeps_lecture(self):
        data = await self.upload(); self.ai.error = StudyAIError('Лимит', 'rate_limit')
        submitted = await self.generate(data)
        self.assertTrue(Path(data['path']).exists())
        row = await self.runtime.store.job(20, submitted.record['id'])
        self.assertEqual(row['error_category'], 'rate_limit')
        self.assertIsNone(await self.runtime.store.active_job(20))

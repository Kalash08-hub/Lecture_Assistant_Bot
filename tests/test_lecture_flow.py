import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.handlers.document import receive_pdf
from app.handlers.study import study_callback
from app.services import lecture_storage, study_ai
from app.states.upload import UploadState


class LectureFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.upload_patch = patch('app.services.uploads.UPLOAD_DIR', self.root)
        self.upload_patch.start()
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=10, user_id=20))
        self.counter = 100
        self.msg = NS(document=NS(file_name='lecture.txt', file_id='file'),
                      from_user=NS(id=20), chat=NS(id=10), answer=AsyncMock(),
                      answer_photo=AsyncMock(side_effect=self.photo))
        self.bot = NS(get_file=AsyncMock(return_value=NS(file_path='remote')),
                      download_file=AsyncMock(side_effect=self.download))

    async def asyncTearDown(self):
        self.upload_patch.stop()
        await self.storage.close()
        self.tmp.cleanup()

    async def photo(self, **kwargs):
        self.counter += 1
        return NS(message_id=self.counter)

    async def download(self, source, destination):
        destination.write_text('Текст лекции', encoding='utf-8')

    async def upload(self):
        await self.state.set_state(UploadState.waiting_for_file)
        await receive_pdf(self.msg, self.bot, self.state)
        return await self.state.get_data()

    def callback(self, mid, action='/abs'):
        return NS(data=action, from_user=NS(id=20), answer=AsyncMock(),
                  message=NS(message_id=mid, chat=NS(id=10), answer=AsyncMock(), answer_document=AsyncMock()))

    async def test_same_name_old_buttons_and_both_modes(self):
        first = await self.upload()
        second = await self.upload()
        self.assertNotEqual(first['lecture_path'], second['lecture_path'])
        self.assertEqual(Path(second['lecture_path']).parent, self.root / '20')
        self.assertFalse(Path(first['lecture_path']).exists())
        self.assertIsNone(await self.state.get_state())
        with patch('app.handlers.study.generate_study_material', new=AsyncMock(return_value='Результат')) as ai:
            old = self.callback(first['lecture_message_id'])
            await study_callback(old, self.state)
            ai.assert_not_awaited()
            for action in ('/abs', '/short'):
                await study_callback(self.callback(second['lecture_message_id'], action), self.state)
            self.assertEqual([c.args for c in ai.await_args_list],
                             [(second['lecture_path'], 'outline'), (second['lecture_path'], 'short')])
        self.assertEqual((await self.state.get_data())['lecture_path'], second['lecture_path'])
        self.assertTrue(Path(second['lecture_path']).exists())

    async def test_failed_upload_keeps_previous(self):
        first = await self.upload()
        self.bot.download_file.side_effect = OSError('disk full')
        await self.upload()
        self.assertEqual((await self.state.get_data())['lecture_path'], first['lecture_path'])
        self.assertEqual(len(list((self.root / '20').iterdir())), 1)

    async def test_replacement_during_generation(self):
        first = await self.upload()
        started, finish = asyncio.Event(), asyncio.Event()
        async def generate(path, mode):
            started.set()
            await finish.wait()
            self.assertTrue(Path(path).exists())
            return 'Конспект'
        with patch('app.handlers.study.generate_study_material', side_effect=generate):
            task = asyncio.create_task(study_callback(self.callback(first['lecture_message_id']), self.state))
            await started.wait()
            second = await self.upload()
            self.assertTrue(Path(first['lecture_path']).exists())
            finish.set()
            await task
        self.assertFalse(Path(first['lecture_path']).exists())
        self.assertTrue(Path(second['lecture_path']).exists())

    async def test_restart_unsupported_and_long_response(self):
        cb = self.callback(1)
        with patch('app.handlers.study.generate_study_material', new=AsyncMock(return_value='Я' * 5000)) as ai:
            await study_callback(cb, self.state)
            ai.assert_not_awaited()
            self.msg.document.file_name = 'lecture.docx'
            doc = await self.upload()
            await study_callback(self.callback(doc['lecture_message_id']), self.state)
            ai.assert_not_awaited()
            self.msg.document.file_name = 'lecture.txt'
            txt = await self.upload()
            cb = self.callback(txt['lecture_message_id'])
            await study_callback(cb, self.state)
            cb.message.answer_document.assert_awaited_once()
            self.assertEqual(cb.message.answer_document.await_args.args[0].data.decode(), 'Я' * 5000)

    async def test_ai_error_keeps_lecture(self):
        data = await self.upload()
        with patch('app.handlers.study.generate_study_material', side_effect=study_ai.StudyAIError('Лимит')):
            await study_callback(self.callback(data['lecture_message_id']), self.state)
        self.assertTrue(Path(data['lecture_path']).exists())
        self.assertEqual(lecture_storage._in_use[data['lecture_path']], 0)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        study_ai._semaphore = None

    async def test_txt_pdf_payload_and_tls(self):
        with tempfile.TemporaryDirectory() as directory:
            txt, pdf = Path(directory) / 'a.txt', Path(directory) / 'a.pdf'
            txt.write_text('Определение и формула', encoding='utf-8')
            pdf.write_bytes(b'%PDF')
            responses = [ {'access_token': 'test'}, {'choices': [{'message': {'content': 'Ответ'}}]} ]
            with patch.dict('os.environ', {'GIGACHAT_CREDENTIALS': 'test', 'GIGACHAT_CA_BUNDLE_FILE': ''}), patch.object(study_ai, '_request', new=AsyncMock(side_effect=responses)) as request:
                self.assertEqual(await study_ai.generate_study_material(txt, 'outline'), 'Ответ')
                payload = request.await_args.kwargs['json']
                self.assertIn('Определение и формула', payload['messages'][1]['content'])
                self.assertNotIn('attachments', payload['messages'][1])
            responses = [{'access_token': 'test'}, {'id': 'attachment'}, {'choices': [{'message': {'content': 'Выжимка'}}]}]
            # Mock cleanup too: no network traffic in tests.
            class Cleanup:
                async def __aenter__(self):
                    return NS(read=AsyncMock())
                async def __aexit__(self, *args):
                    pass
            with patch.dict('os.environ', {'GIGACHAT_CREDENTIALS': 'test', 'GIGACHAT_CA_BUNDLE_FILE': ''}), patch.object(study_ai, '_request', new=AsyncMock(side_effect=responses)) as request, patch('aiohttp.ClientSession.post', return_value=Cleanup()):
                await study_ai.generate_study_material(pdf, 'short')
                self.assertEqual(request.await_args.kwargs['json']['messages'][1]['attachments'], ['attachment'])

    async def test_http_errors_and_timeout(self):
        class Response:
            def __init__(self, status):
                self.status = status
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        for url, stage in ((study_ai.AUTH_URL, 'авторизация'),
                           (study_ai.API_URL + '/files', 'загрузка файла'),
                           (study_ai.API_URL + '/chat/completions', 'генерация')):
            session = NS(request=lambda *args, **kwargs: Response(400))
            with self.assertRaisesRegex(study_ai.StudyAIError, stage + ', HTTP 400'):
                await study_ai._request(session, 'POST', url)
        for status in (401, 402, 413, 422, 429, 503):
            session = NS(request=lambda *args, **kwargs: Response(status))
            with self.assertRaises(study_ai.StudyAIError):
                await study_ai._request(session, 'POST', 'unused')
        with patch.dict('os.environ', {'GIGACHAT_CREDENTIALS': 'test'}), patch.object(study_ai, '_generate', side_effect=asyncio.TimeoutError):
            with self.assertRaisesRegex(study_ai.StudyAIError, 'не ответил вовремя'):
                await study_ai.generate_study_material('lecture.txt', 'short')

    async def test_single_concurrent_generation(self):
        count, peak = 0, 0
        async def generate(*args):
            nonlocal count, peak
            count += 1
            peak = max(peak, count)
            await asyncio.sleep(0.01)
            count -= 1
            return 'ok'
        with patch.dict('os.environ', {'GIGACHAT_CREDENTIALS': 'test'}), patch.object(study_ai, '_generate', side_effect=generate):
            await asyncio.gather(*(study_ai.generate_study_material('lecture.txt', 'short') for _ in range(3)))
        self.assertEqual(peak, 1)


if __name__ == '__main__':
    unittest.main()

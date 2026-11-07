import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from app.database.store import Store
from app.services.gigachat import AUTH_URL, API_URL, GigaChatClient, StudyAIError
from app.services.study_ai import StudyAI, validate_result
from helpers import CARDS, QUIZ, FakeClient, test_settings, write_fixture


class Response:
    def __init__(self, status=200, data=None):
        self.status, self.data = status, data
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self):
        await asyncio.sleep(0)
        return self.data
    async def read(self): return b''


class HttpSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception): raise item
        return item


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_txt_pdf_payload_and_tls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for ext in ('.txt', '.pdf'):
                p = root / ('lecture' + ext); write_fixture(p)
                client = FakeClient(test_settings(root)); material = await StudyAI(client).generate(p, 'outline')
                self.assertEqual(material.usage, {'total_tokens': 20})
                user = client.payloads[0]['messages'][1]
                if ext == '.txt': self.assertIn('Плотность', user['content']); self.assertNotIn('attachments', user)
                else: self.assertIn('attachments', user); self.assertEqual(len(client.deleted), 1)
            context = test_settings(root).ssl_context()
            import ssl
            self.assertTrue(context.check_hostname); self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    async def test_http_errors_and_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for status, category in ((401, 'access'), (402, 'balance'), (413, 'document'), (422, 'document'), (429, 'rate_limit'), (503, 'unavailable'), (400, 'request')):
                session = HttpSession([Response(status)])
                client = GigaChatClient(test_settings(root), session=session)
                with self.assertRaises(StudyAIError) as caught:
                    await client.request_json('POST', API_URL + '/chat/completions', stage='генерация')
                self.assertEqual(caught.exception.category, category); self.assertEqual(len(session.calls), 1)
            session = HttpSession([asyncio.TimeoutError()])
            client = GigaChatClient(test_settings(root), session=session)
            with self.assertRaises(StudyAIError) as caught:
                await client.request_json('POST', API_URL + '/chat/completions', stage='генерация')
            self.assertIn(caught.exception.category, {'timeout', 'response'})

    async def test_oauth_cached_concurrent_refresh_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            session = HttpSession([Response(data={'access_token': 'one', 'expires_at': (time.time() + 1800)*1000}),
                                   Response(data={'access_token': 'two', 'expires_at': time.time() + 1800})])
            client = GigaChatClient(test_settings(Path(directory)), session=session)
            values = await asyncio.gather(*(client.access_token() for _ in range(10)))
            self.assertEqual(set(values), {'one'}); self.assertEqual(len(session.calls), 1)
            client.expires = time.time() + 10
            self.assertEqual(await client.access_token(), 'two'); self.assertEqual(len(session.calls), 2)

    async def test_safe_oauth_retry_but_no_generation_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            session = HttpSession([Response(503), Response(data={'access_token': 'one', 'expires_at': time.time()+1800}), asyncio.TimeoutError()])
            client = GigaChatClient(test_settings(Path(directory)), session=session)
            self.assertEqual(await client.access_token(), 'one')
            with self.assertRaises(StudyAIError): await client.complete({})
            self.assertEqual(len(session.calls), 3)

    async def test_remote_cleanup_http_failure_is_persisted_and_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); db = Store(root/'db'); await db.open()
            try:
                file_id = '86f6b766-924e-4c61-81f8-f77f1f17b26b'
                await db.schedule_remote_cleanup(file_id)
                session = HttpSession([Response(500), Response(200)])
                client = GigaChatClient(test_settings(root), db, session=session)
                client.token, client.expires = 'one', time.time()+1800
                await client.cleanup_file(file_id)
                row = await db._one('SELECT * FROM remote_cleanup WHERE file_id=?', (file_id,))
                self.assertEqual(row['attempts'], 1)
                await db.connection.execute('UPDATE remote_cleanup SET next_attempt=0')
                await client.retry_cleanup()
                self.assertIsNone(await db._one('SELECT * FROM remote_cleanup'))
            finally: await db.close()

    async def test_cleanup_failure_does_not_hide_success_and_retries_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root/'sample.pdf'; write_fixture(path)
            db = Store(root/'db'); await db.open()
            file_id = '86f6b766-924e-4c61-81f8-f77f1f17b26b'
            session = HttpSession([
                Response(data={'access_token': 'one', 'expires_at': time.time()+1800}),
                Response(data={'id': file_id}),
                Response(data={'choices': [{'message': {'content': 'Готовый конспект'}}]}),
                Response(503), Response(503), Response(503),
            ])
            client = GigaChatClient(test_settings(root), db, session=session)
            try:
                result = await StudyAI(client).generate(path, 'outline')
                self.assertEqual(result.content, 'Готовый конспект')
                if client.cleanup_tasks:
                    await asyncio.gather(*client.cleanup_tasks)
                for _ in range(3):
                    await db.connection.execute('UPDATE remote_cleanup SET next_attempt=0')
                    await client.retry_cleanup()
                count = sum(url.endswith('/delete') for _, url, _ in session.calls)
                self.assertEqual(count, 3)
                self.assertEqual((await db._one('SELECT * FROM remote_cleanup'))['attempts'], 3)
            finally:
                await client.close(); await db.close()

    async def test_structured_generation_uses_source_and_returns_validated_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); p = root/'lecture.txt'; write_fixture(p)
            for mode, data in (('quiz', QUIZ), ('cards', CARDS)):
                client = FakeClient(test_settings(root))
                client.response = {'choices': [{'message': {'content': json.dumps(data, ensure_ascii=False)}}]}
                result = await StudyAI(client).generate(p, mode)
                self.assertEqual(json.loads(result.content), data)
                self.assertIn('Плотность', client.payloads[0]['messages'][1]['content'])

    async def test_invalid_model_response_and_structured_schema(self):
        self.assertIn('questions', json.loads(validate_result(json.dumps(QUIZ), 'quiz')))
        self.assertIn('cards', json.loads(validate_result(json.dumps(CARDS), 'cards')))
        invalid = [ {'questions': []}, {'questions': [{**QUIZ['questions'][0], 'correct_index': 3}]},
                    {'questions': [{**QUIZ['questions'][0], 'correct_index': True}]},
                    {'questions': [{**QUIZ['questions'][0], 'options': ['A', 'a']}]},
                    {'questions': [{**QUIZ['questions'][0], 'question': ' '}]}, {'cards': [{'question': ' ', 'answer':'x'}]} ]
        for data in invalid:
            mode = 'quiz' if 'questions' in data else 'cards'
            with self.assertRaises(StudyAIError): validate_result(json.dumps(data), mode)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); p = root/'a.txt'; write_fixture(p); client = FakeClient(test_settings(root))
            for response in ({}, {'choices': []}, {'choices': [{'message': {'content': ''}}]}):
                client.response = response
                with self.assertRaises(StudyAIError): await StudyAI(client).generate(p, 'outline')

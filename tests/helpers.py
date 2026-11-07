import asyncio
import json
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from zipfile import ZipFile

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, File, Message, User

from app.config import Settings
from app.runtime import Runtime
from app.services.prompts import PROMPTS
from app.services.study_ai import GeneratedMaterial

ODT_CONTENT = '''<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"><office:body><office:text><text:h>Плотность</text:h><text:p>Формула:<text:s/>ρ = m / V.<text:line-break/>Масса<text:tab/>10 кг.</text:p><text:list><text:list-item><text:p>Объём 2 м³.</text:p></text:list-item></text:list><table:table><table:table-row><table:table-cell><text:p>Результат: 5 кг/м³.</text:p></table:table-cell></table:table-row></table:table></office:text></office:body></office:document-content>'''
ODP_CONTENT = '''<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:presentation><draw:page><text:h>Плотность</text:h><text:p>Формула: <text:span>ρ = m / V</text:span></text:p></draw:page><draw:page><text:p>Пример: 10 / 2 = 5 кг/м³</text:p></draw:page></office:presentation></office:body></office:document-content>'''
QUIZ = {'questions': [{'question': 'Что такое плотность?', 'options': ['Масса / объём', 'Только масса'], 'correct_index': 0, 'explanation': 'В лекции дана формула ρ = m / V.'},
                      {'question': 'Единица СИ?', 'options': ['кг/м³', 'кг'], 'correct_index': 0, 'explanation': 'Килограмм на кубический метр.'}]}
CARDS = {'cards': [{'question': 'Что такое плотность?', 'answer': 'Отношение массы к объёму.'}, {'question': 'Формула?', 'answer': 'ρ = m / V.'}]}


def zip_file(path: Path, entry: str, content: str):
    with ZipFile(path, 'w') as archive:
        archive.writestr(entry, content)


def write_fixture(path: Path) -> None:
    if path.suffix.lower() == '.odt':
        zip_file(path, 'content.xml', ODT_CONTENT)
    elif path.suffix.lower() == '.odp':
        zip_file(path, 'content.xml', ODP_CONTENT)
    elif path.suffix.lower() == '.docx':
        from docx import Document
        d = Document(); d.add_paragraph('До таблицы'); d.add_table(rows=1, cols=1).cell(0, 0).text = 'Текст таблицы'; d.add_paragraph('После таблицы'); d.save(path)
    elif path.suffix.lower() == '.pptx':
        zip_file(path, 'ppt/presentation.xml', '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:sldIdLst><p:sldId id="1"/></p:sldIdLst></p:presentation>')
    elif path.suffix.lower() == '.ppt':
        path.write_bytes(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1test')
    elif path.suffix.lower() == '.pdf':
        path.write_bytes(b'%PDF-1.7\n/Type /Catalog\n%%EOF')
    else:
        path.write_text('Текст лекции. Плотность — отношение массы к объёму.', encoding='utf-8')


class FakeAI:
    def __init__(self):
        self.calls = []
        self.gate = None
        self.started = asyncio.Event()
        self.error = None
        self.text = 'Учебный материал'

    async def generate(self, path, mode):
        self.calls.append((str(path), mode))
        self.started.set()
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        text = json.dumps(QUIZ if mode == 'quiz' else CARDS, ensure_ascii=False) if mode in {'quiz', 'cards'} else self.text
        return GeneratedMaterial(text, 'test-model', {'total_tokens': 10})


class FakeClient:
    def __init__(self, settings):
        self.settings = settings
        self.uploads = []
        self.payloads = []
        self.deleted = []
        self.response = None

    async def upload(self, path, mime):
        self.uploads.append((path, mime, path.read_bytes()))
        return '86f6b766-924e-4c61-81f8-f77f1f17b26b'

    async def complete(self, payload):
        self.payloads.append(payload)
        if self.response is not None:
            return self.response
        return {'choices': [{'message': {'content': 'Материал'}}], 'usage': {'total_tokens': 20}}

    def clean_later(self, file_id):
        self.deleted.append(file_id)


class FakeTelegram(BaseSession):
    """Real aiogram Bot/Dispatcher, fake only the HTTP transport."""
    def __init__(self):
        super().__init__()
        self.calls = []
        self.files = {}
        self.sequence = 100
        self.fail_admin = None

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = method.__class__.__name__
        if name == 'AnswerCallbackQuery':
            return True
        if name == 'GetFile':
            return File(file_id=method.file_id, file_unique_id='unique', file_path='remote/' + method.file_id,
                        file_size=len(self.files.get(method.file_id, b'')))
        if name == 'GetMe':
            return User(id=bot.id, is_bot=True, first_name='Test bot', username='test_bot')
        if name == 'SetMyCommands':
            return True
        if name in {'SendMessage', 'SendPhoto', 'SendDocument'}:
            if self.fail_admin == method.chat_id:
                from aiogram.exceptions import TelegramForbiddenError
                raise TelegramForbiddenError(method=method, message='Mocked admin delivery blocked')
            self.sequence += 1
            return Message(message_id=self.sequence, date=datetime.now(timezone.utc), chat=Chat(id=int(method.chat_id), type='private'),
                           from_user=User(id=bot.id, is_bot=True, first_name='Bot'), text=getattr(method, 'text', None))
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        data = self.files[url.rsplit('/', 1)[-1]]
        for offset in range(0, len(data), chunk_size):
            yield data[offset:offset + chunk_size]

    def texts(self):
        return [getattr(call, 'text', None) or getattr(call, 'caption', None) or '' for call in self.calls]


async def wait_until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(.005)


async def job_done(store, owner, job_id):
    row = await store.job(owner, job_id)
    return row and row['status'] not in {'queued', 'running'}


def test_settings(root: Path, **kwargs):
    settings = Settings(bot_token='123456:TEST_TOKEN', credentials='test', model='test-model', upload_dir=root / 'files',
                        database_path=root / 'data/bot.sqlite3', cleanup_interval=3600)
    return replace(settings, **kwargs)

import asyncio
import io
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from zipfile import ZipFile

from docx import Document
from app.database.store import AdmissionError, Store
from app.services.archive_text import DocumentError, MAX_UNPACKED_SIZE, read_xml, bounded_join
from app.services.content import validate_file
from app.services.delivery import docx_bytes, prefix, utf16_length
from app.services.document_text import extract_docx_text
from app.services.lecture_storage import LectureFiles
from app.services.presentation_text import extract_odp_text
from app.services.prompts import PROMPT_VERSION
from helpers import ODP_CONTENT, ODT_CONTENT, test_settings, write_fixture, zip_file


class DocumentFiles(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.settings = test_settings(self.root, max_upload_bytes=100)
        self.db = Store(self.settings.database_path); await self.db.open()
        self.files = LectureFiles(self.db, self.settings)
        self.bot = NS(get_file=AsyncMock(return_value=NS(file_path='remote')), download_file=AsyncMock(side_effect=self.download))
        self.payload = b'Normal lecture text'

    async def asyncTearDown(self):
        await self.db.close(); self.tmp.cleanup()

    async def download(self, source, destination): destination.write(self.payload)

    async def save(self, owner=1, declared=None):
        return await self.files.save(owner, 'lecture.txt', NS(file_id='file', file_size=declared), self.bot)

    async def test_declared_and_actual_size_partial_cleanup_previous_survives(self):
        first = await self.save()
        with self.assertRaises(DocumentError): await self.save(declared=101)
        self.assertEqual(self.bot.download_file.await_count, 1)
        self.payload = b'x'*101
        with self.assertRaises(DocumentError): await self.save()
        self.payload = b'short'
        with self.assertRaises(DocumentError): await self.save(declared=10)
        rows, count = await self.db.lectures(1)
        self.assertEqual(count, 1); self.assertTrue(Path(first['path']).exists())
        self.assertEqual(len(list((self.settings.upload_dir/'1').iterdir())), 1)

    async def test_cancel_download_removes_partial_file(self):
        started = asyncio.Event(); gate = asyncio.Event()
        async def slow(source, destination):
            destination.write(b'partial'); started.set(); await gate.wait()
        self.bot.download_file.side_effect = slow
        task = asyncio.create_task(self.save()); await started.wait()
        self.assertTrue(await self.files.cancel_upload(1))
        self.assertTrue(task.cancelled())
        self.assertEqual(list((self.settings.upload_dir/'1').iterdir()), [])
        self.assertEqual((await self.db.lectures(1))[1], 0)

    async def test_upload_timeout_and_quota_cleanup(self):
        self.files.settings = replace(self.settings, upload_timeout=.02)
        async def slow(*args, **kwargs): await asyncio.sleep(10)
        self.bot.download_file.side_effect = slow
        with self.assertRaises(TimeoutError): await self.save()
        self.assertEqual(list((self.settings.upload_dir/'1').iterdir()), [])
        self.bot.download_file.side_effect = self.download
        self.files.settings = replace(self.settings, max_user_files=1)
        first = await self.save()
        with self.assertRaises(AdmissionError): await self.save()
        self.assertTrue(Path(first['path']).exists()); self.assertEqual(len(list((self.settings.upload_dir/'1').iterdir())), 1)
        self.files.settings = replace(self.settings, max_user_bytes=len(self.payload))
        with self.assertRaises(AdmissionError): await self.save()

    async def test_expiry_orphans_symlinks_and_active_files(self):
        row = await self.save()
        job = await self.db.admit(1, row['id'], 'outline', 'model', PROMPT_VERSION, 1, 10)
        await self.db.connection.execute('UPDATE lectures SET expires=0')
        orphan = self.settings.upload_dir/'1'/f'{uuid4()}.txt'; orphan.write_text('orphan'); os.utime(orphan, (0,0))
        unrelated = self.root/'keep.txt'; unrelated.write_text('must stay')
        symlink = self.settings.upload_dir/'1'/f'{uuid4()}.txt'; symlink.symlink_to(unrelated)
        await self.files.cleanup()
        self.assertFalse(orphan.exists()); self.assertTrue(unrelated.exists()); self.assertTrue(Path(row['path']).exists())
        await self.db.set_job_status(job['id'], 'cancelled')
        await self.files.cleanup()
        self.assertFalse(Path(row['path']).exists()); self.assertIsNone(await self.db.lecture(1, row['id']))

    async def test_docx_order_empty_corrupt_and_wrong_extension(self):
        p = self.root/'sample.docx'; write_fixture(p)
        self.assertEqual(extract_docx_text(p), 'До таблицы\nТекст таблицы\nПосле таблицы')
        validate_file(p)
        d = Document(); d.save(p)
        with self.assertRaises(DocumentError): extract_docx_text(p)
        p.write_bytes(b'not a zip')
        with self.assertRaises(DocumentError): validate_file(p)
        p.write_bytes(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1encrypted')
        with self.assertRaises(DocumentError): validate_file(p)
        p = self.root/'bad.pdf'; p.write_bytes(b'not a pdf')
        with self.assertRaises(DocumentError): validate_file(p)
        p.write_bytes(b'%PDF-1.7 /Encrypt')
        with self.assertRaisesRegex(DocumentError, 'зашифрован'): validate_file(p)

    async def test_zip_bombs_entities_corrupt_and_text_size(self):
        p = self.root/'sample.odp'
        zip_file(p, 'content.xml', ODP_CONTENT)
        with patch('app.services.archive_text.MAX_UNPACKED_SIZE', 10):
            with self.assertRaises(DocumentError): extract_odp_text(p)
        zip_file(p, 'content.xml', '<!DOCTYPE x [<!ENTITY evil "evil">]><x>&evil;</x>')
        with self.assertRaises(DocumentError): extract_odp_text(p)
        zip_file(p, 'content.xml', '<x>'*65 + '</x>'*65)
        with self.assertRaisesRegex(DocumentError, 'сложная'): extract_odp_text(p)
        zip_file(p, 'content.xml', '<broken')
        with self.assertRaises(DocumentError): extract_odp_text(p)
        with patch('app.services.archive_text.MAX_TEXT_SIZE', 5):
            with self.assertRaises(DocumentError): bounded_join(iter(['one', 'two']))
        # Corrupt compressed data must be a user-facing document error.
        with ZipFile(p, 'w', compression=8) as z: z.writestr('content.xml', ODP_CONTENT)
        data=bytearray(p.read_bytes()); offset=30+len('content.xml'); data[offset:offset+4]=b'BAD!';p.write_bytes(data)
        with self.assertRaises(DocumentError): extract_odp_text(p)

    async def test_docx_export_cyrillic_and_utf16(self):
        data = docx_bytes('Лекция по физике', 'Определение плотности\nρ = m / V\nПример: 5 кг/м³.')
        d = Document(io.BytesIO(data)); text = '\n'.join(p.text for p in d.paragraphs)
        self.assertIn('Лекция по физике', text); self.assertIn('5 кг/м³', text)
        self.assertEqual(utf16_length('Я😀'), 3)
        self.assertEqual(prefix('Я😀', 2), 'Я')

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_lecture_flow as flow
from app.services.document_text import DocumentTextError, extract_odt_text
from app.services.study_ai import StudyAI
from app.services.gigachat import StudyAIError
from helpers import FakeClient, ODT_CONTENT as CONTENT, test_settings, zip_file


def write_odt(path, content=CONTENT):
    zip_file(path, 'content.xml', content)


class OdtFlow(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = flow.LectureFlow.asyncSetUp
    asyncTearDown = flow.LectureFlow.asyncTearDown
    photo = flow.LectureFlow.photo
    download = flow.LectureFlow.download
    upload = flow.LectureFlow.upload
    generate = flow.LectureFlow.generate

    async def test_upload_and_both_buttons_send_odt_text(self):
        self.msg.document.file_name = 'lecture.ODT'; row = await self.upload()
        self.assertEqual(Path(row['path']).suffix, '.odt')
        for mode in ('outline', 'short'):
            await self.generate(row, mode)
        self.assertEqual([mode for _, mode in self.ai.calls], ['outline', 'short'])
        client = FakeClient(self.runtime.settings); await StudyAI(client).generate(Path(row['path']), 'outline')
        self.assertIn('Формула: ρ = m / V.', client.payloads[0]['messages'][1]['content'])
        self.assertEqual(client.uploads, [])
        self.assertTrue(Path(row['path']).is_file())


class OdtReader(unittest.IsolatedAsyncioTestCase):
    async def test_paragraphs_lists_tables_and_whitespace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lecture.odt'; write_odt(path)
            self.assertEqual(extract_odt_text(path), 'Плотность\nФормула: ρ = m / V.\nМасса\t10 кг.\nОбъём 2 м³.\nРезультат: 5 кг/м³.')

    async def test_invalid_empty_oversized_odt_never_calls_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'lecture.odt'
            scenarios = (b'not a zip', '<broken', CONTENT.replace('<office:text>', '<office:presentation>').replace('</office:text>', '</office:presentation>'), CONTENT[:CONTENT.index('<text:h>')] + '</office:text></office:body></office:document-content>')
            for content in scenarios:
                if isinstance(content, bytes): path.write_bytes(content)
                else: write_odt(path, content)
                client = FakeClient(test_settings(root))
                with self.assertRaises(StudyAIError): await StudyAI(client).generate(path, 'outline')
                self.assertEqual(client.payloads, [])
            write_odt(path)
            with patch('app.services.document_text.MAX_XML_SIZE', 10):
                with self.assertRaisesRegex(DocumentTextError, 'больш'):
                    extract_odt_text(path)

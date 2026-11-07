import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile

import test_lecture_flow as flow
from app.handlers.study import study_callback
from app.services import study_ai
from app.services.document_text import DocumentTextError, extract_odt_text

CONTENT = '''<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">
 <office:body><office:text>
 <text:h>Плотность</text:h>
 <text:p>Формула:<text:s/>ρ = m / V.<text:line-break/>Масса<text:tab/>10 кг.</text:p>
 <text:list><text:list-item><text:p>Объём 2 м³.</text:p></text:list-item></text:list>
 <table:table><table:table-row><table:table-cell><text:p>Результат: 5 кг/м³.</text:p></table:table-cell></table:table-row></table:table>
 </office:text></office:body></office:document-content>'''


def write_odt(path, content=CONTENT):
    with ZipFile(path, 'w') as archive:
        archive.writestr('content.xml', content)


class OdtFlow(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = flow.LectureFlow.asyncSetUp
    asyncTearDown = flow.LectureFlow.asyncTearDown
    photo = flow.LectureFlow.photo
    callback = flow.LectureFlow.callback
    upload = flow.LectureFlow.upload

    async def download(self, source, destination):
        write_odt(destination)

    async def test_upload_and_both_buttons_send_odt_text(self):
        self.msg.document.file_name = 'lecture.ODT'
        data = await self.upload()
        self.assertEqual(Path(data['lecture_path']).suffix, '.odt')
        self.assertIsNone(await self.state.get_state())
        async def request(session, method, url, **kwargs):
            if url == study_ai.AUTH_URL:
                return {'access_token': 'test'}
            self.assertTrue(url.endswith('/chat/completions'))
            user = kwargs['json']['messages'][1]
            self.assertIn('Формула: ρ = m / V.', user['content'])
            self.assertIn('Объём 2 м³.', user['content'])
            self.assertIn('Результат: 5 кг/м³.', user['content'])
            self.assertNotIn('attachments', user)
            return {'choices': [{'message': {'content': 'Материал'}}]}
        study_ai._semaphore = None
        with patch.dict('os.environ', {'GIGACHAT_CREDENTIALS': 'test', 'GIGACHAT_CA_BUNDLE_FILE': ''}), patch.object(study_ai, '_request', side_effect=request) as api:
            for action in ('/abs', '/short'):
                cb = self.callback(data['lecture_message_id'], action)
                await study_callback(cb, self.state)
                self.assertEqual(cb.message.answer.await_args.args[0], 'Материал')
            self.assertEqual(api.await_count, 4)
        self.assertEqual((await self.state.get_data())['lecture_path'], data['lecture_path'])
        self.assertTrue(Path(data['lecture_path']).is_file())


class OdtReader(unittest.IsolatedAsyncioTestCase):
    async def test_paragraphs_lists_tables_and_whitespace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lecture.odt'
            write_odt(path)
            self.assertEqual(extract_odt_text(path),
                             'Плотность\nФормула: ρ = m / V.\nМасса\t10 кг.\nОбъём 2 м³.\nРезультат: 5 кг/м³.')

    async def test_invalid_empty_oversized_odt_never_calls_api(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lecture.odt'
            scenarios = (b'not a zip', '<broken', CONTENT.replace('<office:text>', '<office:presentation>').replace('</office:text>', '</office:presentation>'), CONTENT[:CONTENT.index('<text:h>')] + '</office:text></office:body></office:document-content>')
            for content in scenarios:
                if isinstance(content, bytes):
                    path.write_bytes(content)
                else:
                    write_odt(path, content)
                study_ai._semaphore = None
                with patch.dict('os.environ', {'GIGACHAT_CREDENTIALS': 'test', 'GIGACHAT_CA_BUNDLE_FILE': ''}), patch.object(study_ai, '_request', new=AsyncMock()) as api:
                    with self.assertRaises(study_ai.StudyAIError):
                        await study_ai.generate_study_material(path, 'outline')
                    api.assert_not_awaited()
            write_odt(path)
            with patch('app.services.document_text.MAX_XML_SIZE', 10):
                with self.assertRaisesRegex(DocumentTextError, 'слишком большой'):
                    extract_odt_text(path)

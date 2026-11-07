import tempfile
import unittest
from pathlib import Path

import test_lecture_flow as flow
from app.handlers.presentation import receive_presentation
from app.services.gigachat import StudyAIError
from app.services.presentation_text import extract_odp_text
from app.services.study_ai import StudyAI
from helpers import FakeClient, ODP_CONTENT as CONTENT, test_settings, zip_file, write_fixture


def make_odp(path, content=CONTENT):
    zip_file(path, 'content.xml', content)


class PresentationFlow(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = flow.LectureFlow.asyncSetUp
    asyncTearDown = flow.LectureFlow.asyncTearDown
    photo = flow.LectureFlow.photo
    download = flow.LectureFlow.download
    callback = flow.LectureFlow.callback
    generate = flow.LectureFlow.generate

    async def test_presentation_buttons_and_old_message(self):
        for suffix in ('.pdf', '.ppt', '.pptx', '.odp'):
            self.msg.document.file_name = 'slides' + suffix
            await receive_presentation(self.msg, self.bot, self.state, self.runtime)
            rows, _ = await self.runtime.store.lectures(20); row = rows[0]
            for mode in ('outline', 'short'):
                await self.generate(row, mode)
            self.assertTrue(Path(row['path']).exists())
        self.assertEqual(len(self.ai.calls), 8)


class PresentationService(unittest.IsolatedAsyncioTestCase):
    async def test_ppt_and_pptx_upload_mime_attachment_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for suffix, mime in (('.ppt', 'application/vnd.ms-powerpoint'), ('.pptx', 'application/vnd.openxmlformats-officedocument.presentationml.presentation')):
                path = root / ('slides' + suffix); write_fixture(path)
                client = FakeClient(test_settings(root)); ai = StudyAI(client)
                material = await ai.generate(path, 'outline')
                self.assertEqual(material.content, 'Материал')
                self.assertEqual(client.uploads[0][1], mime)
                self.assertEqual(client.payloads[0]['messages'][1]['attachments'], client.deleted)
                self.assertEqual(client.payloads[0]['function_call'], 'auto')

    async def test_odp_text_sent_in_slide_order_without_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'slides.odp'; make_odp(path)
            text = extract_odp_text(path)
            self.assertLess(text.index('Слайд 1'), text.index('Слайд 2'))
            self.assertIn('Формула: ρ = m / V', text)
            client = FakeClient(test_settings(root)); await StudyAI(client).generate(path, 'short')
            self.assertIn(text, client.payloads[0]['messages'][1]['content'])
            self.assertNotIn('attachments', client.payloads[0]['messages'][1])
            self.assertEqual(client.uploads, [])

    async def test_bad_and_empty_odp_do_not_call_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'broken.odp'
            for content in (None, '<broken', CONTENT.replace('<text:h>Плотность</text:h>', '').replace('<text:p>Формула: <text:span>ρ = m / V</text:span></text:p>', '').replace('<text:p>Пример: 10 / 2 = 5 кг/м³</text:p>', '')):
                if content is None:
                    path.write_bytes(b'not a zip')
                else:
                    make_odp(path, content)
                client = FakeClient(test_settings(root))
                with self.assertRaises(StudyAIError):
                    await StudyAI(client).generate(path, 'short')
                self.assertEqual(client.payloads, [])

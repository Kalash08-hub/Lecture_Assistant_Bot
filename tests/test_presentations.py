import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile

from app.handlers.presentation import receive_presentation
from app.handlers.study import study_callback
from app.services import study_ai
from app.services.presentation_text import extract_odp_text
import test_lecture_flow as flow

CONTENT = '''<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:presentation>
 <draw:page><text:h>Плотность</text:h><text:p>Формула: <text:span>ρ = m / V</text:span></text:p></draw:page>
 <draw:page><text:p>Пример: 10 / 2 = 5 кг/м³</text:p></draw:page>
 </office:presentation></office:body></office:document-content>'''


def make_odp(path, content=CONTENT):
    with ZipFile(path, 'w') as archive:
        archive.writestr('content.xml', content)


class PresentationFlow(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = flow.LectureFlow.asyncSetUp
    asyncTearDown = flow.LectureFlow.asyncTearDown
    photo = flow.LectureFlow.photo
    download = flow.LectureFlow.download
    callback = flow.LectureFlow.callback
    async def test_presentation_buttons_and_old_message(self):
        with patch('app.handlers.study.generate_study_material', new=AsyncMock(return_value='Ответ')) as ai:
            previous = None
            for suffix in ('.ppt', '.pptx', '.odp'):
                self.msg.document.file_name = 'slides' + suffix
                await receive_presentation(self.msg, self.bot, self.state)
                data = await self.state.get_data()
                if previous:
                    count = ai.await_count
                    await study_callback(self.callback(previous['lecture_message_id']), self.state)
                    self.assertEqual(ai.await_count, count)
                for action, mode in (('/abs', 'outline'), ('/short', 'short')):
                    await study_callback(self.callback(data['lecture_message_id'], action), self.state)
                    ai.assert_awaited_with(data['lecture_path'], mode)
                self.assertTrue(Path(data['lecture_path']).exists())
                previous = data


class PresentationService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        study_ai._semaphore = None

    async def test_ppt_and_pptx_upload_mime_attachment_and_cleanup(self):
        class Cleanup:
            async def __aenter__(self):
                return NS(read=AsyncMock())
            async def __aexit__(self, *args):
                pass
        with tempfile.TemporaryDirectory() as directory:
            for suffix, mime in (('.ppt', 'application/vnd.ms-powerpoint'), ('.pptx', 'application/vnd.openxmlformats-officedocument.presentationml.presentation')):
                path = Path(directory) / ('slides' + suffix)
                path.write_bytes(b'test upload bytes')
                async def request(session, method, url, **kwargs):
                    if url == study_ai.AUTH_URL:
                        return {'access_token': 'test'}
                    if url.endswith('/files'):
                        form = kwargs['data']
                        field = form._fields[-1]
                        self.assertEqual(field[0]['filename'], path.name)
                        self.assertEqual(field[1]['Content-Type'], mime)
                        self.assertEqual(field[2].read(), b'test upload bytes')
                        return {'id': 'slides-attachment'}
                    self.assertEqual(kwargs['json']['messages'][1]['attachments'], ['slides-attachment'])
                    self.assertEqual(kwargs['json']['function_call'], 'auto')
                    return {'choices': [{'message': {'content': 'Конспект'}}]}
                with patch.dict('os.environ', {'GIGACHAT_CREDENTIALS': 'test', 'GIGACHAT_CA_BUNDLE_FILE': ''}), patch.object(study_ai, '_request', side_effect=request), patch('aiohttp.ClientSession.post', return_value=Cleanup()) as cleanup:
                    self.assertEqual(await study_ai.generate_study_material(path, 'outline'), 'Конспект')
                    cleanup.assert_called_once()
                    self.assertTrue(cleanup.call_args.args[0].endswith('/slides-attachment/delete'))

    async def test_odp_text_sent_in_slide_order_without_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'slides.odp'
            make_odp(path)
            text = extract_odp_text(path)
            self.assertLess(text.index('Слайд 1'), text.index('Слайд 2'))
            self.assertIn('Формула: ρ = m / V', text)
            responses = [{'access_token': 'test'}, {'choices': [{'message': {'content': 'Выжимка'}}]}]
            with patch.dict('os.environ', {'GIGACHAT_CREDENTIALS': 'test', 'GIGACHAT_CA_BUNDLE_FILE': ''}), patch.object(study_ai, '_request', new=AsyncMock(side_effect=responses)) as request:
                await study_ai.generate_study_material(path, 'short')
                user = request.await_args.kwargs['json']['messages'][1]
                self.assertIn(text, user['content'])
                self.assertNotIn('attachments', user)
                self.assertEqual(request.await_count, 2)

    async def test_bad_and_empty_odp_do_not_call_api(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'broken.odp'
            for content in (None, '<broken', CONTENT.replace('<text:h>Плотность</text:h>', '').replace('<text:p>Формула: <text:span>ρ = m / V</text:span></text:p>', '').replace('<text:p>Пример: 10 / 2 = 5 кг/м³</text:p>', '')):
                if content is None:
                    path.write_bytes(b'not a zip')
                else:
                    make_odp(path, content)
                with patch.dict('os.environ', {'GIGACHAT_CREDENTIALS': 'test', 'GIGACHAT_CA_BUNDLE_FILE': ''}), patch.object(study_ai, '_request', new=AsyncMock()) as request:
                    with self.assertRaises(study_ai.StudyAIError):
                        await study_ai.generate_study_material(path, 'short')
                    request.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()

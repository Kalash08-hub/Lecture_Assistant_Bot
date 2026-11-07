import asyncio
import io
import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Document, InaccessibleMessage, Message, Update, User
from docx import Document as WordDocument

from app.config import PROJECT_ROOT
from app.keyboards.inline import Inline_start, Inline_in_start, Inline_processing_file
from app.main import create_dispatcher
from app.runtime import Runtime
from app.services.prompts import PROMPT_VERSION
from app.states.upload import UploadState
from helpers import CARDS, QUIZ, FakeAI, FakeTelegram, test_settings, write_fixture


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.transport=FakeTelegram();self.bot=Bot('123456:TEST_TOKEN',session=self.transport)
        self.ai=FakeAI();self.runtime=Runtime(test_settings(self.root,admin_id=777),self.bot,self.ai)
        await self.runtime.open(workers=False)
        self.dp=create_dispatcher(self.runtime);self.sequence=1

    async def asyncTearDown(self):
        await self.runtime.close();await self.dp.storage.close();await self.bot.session.close();self.tmp.cleanup()

    def message(self,text=None,owner=20,document=None):
        self.sequence+=1
        return Message(message_id=self.sequence,date=datetime.now(timezone.utc),chat=Chat(id=owner,type='private'),
                       from_user=User(id=owner,is_bot=False,first_name='Student'),text=text,document=document)

    async def send(self,text,owner=20):
        message=self.message(text,owner)
        await self.dp.feed_update(self.bot,Update(update_id=self.sequence,message=message))

    async def click(self,data,owner=20,message_id=None,inaccessible=False):
        self.sequence+=1
        chat=Chat(id=owner,type='private')
        message=InaccessibleMessage(chat=chat,message_id=message_id or self.sequence,date=0) if inaccessible else Message(
            message_id=message_id or self.sequence,date=datetime.now(timezone.utc),chat=chat,
            from_user=User(id=self.bot.id,is_bot=True,first_name='Bot'))
        callback=CallbackQuery(id=str(self.sequence),from_user=User(id=owner,is_bot=False,first_name='Student'),
                               chat_instance='test',message=message,data=data)
        await self.dp.feed_update(self.bot,Update(update_id=self.sequence,callback_query=callback))

    async def state(self,owner=20):
        return await self.dp.fsm.storage.get_state(StorageKey(bot_id=self.bot.id,chat_id=owner,user_id=owner))

    async def lecture(self,owner=20,name='Лекция.txt'):
        root=self.runtime.settings.upload_dir/str(owner);root.mkdir(parents=True,exist_ok=True)
        from uuid import uuid4
        p=root/(str(uuid4())+Path(name).suffix.lower());write_fixture(p)
        return await self.runtime.store.add_lecture(owner,name,p,'hash-'+str(owner),p.stat().st_size,30,20,1000000)

    async def result(self,mode='outline',owner=20,content='Результат'):
        lecture=await self.lecture(owner)
        job=await self.runtime.store.admit(owner,lecture['id'],mode,self.runtime.settings.model,PROMPT_VERSION,owner,100)
        return await self.runtime.store.finish(job,content,None)

    async def test_exact_asset_case_and_all_static_buttons_are_routed_and_acknowledged(self):
        for path in (PROJECT_ROOT/'app').rglob('*.py'):
            source=path.read_text()
            for filename in re.findall(r"assets/([^'\"\s]+\.png)",source):
                self.assertIn(filename,{p.name for p in (PROJECT_ROOT/'assets').iterdir()})
        for markup in (Inline_start,Inline_in_start,Inline_processing_file):
            for row in markup.inline_keyboard:
                for button in row:
                    before=len(self.transport.calls)
                    await self.click(button.callback_data)
                    calls=self.transport.calls[before:]
                    self.assertTrue(any(c.__class__.__name__=='AnswerCallbackQuery' for c in calls),button.callback_data)
                    self.assertTrue(any(c.__class__.__name__.startswith('Send') for c in calls),button.callback_data)

    async def test_start_cancel_and_menu_preserve_lecture(self):
        row=await self.lecture();await self.send('/start');await self.click('/add_file')
        self.assertEqual(await self.state(),UploadState.waiting_for_file.state)
        await self.send('/cancel');self.assertIsNone(await self.state())
        self.assertIsNotNone(await self.runtime.store.lecture(20,row['id']))
        await self.click('/point');await self.click('/start');self.assertIsNone(await self.state())

    async def test_support_text_confirmation_cooldown_and_cancel(self):
        await self.send('/help');self.assertEqual(await self.state(),UploadState.waiting_for_feedback.state)
        await self.send('Нужна помощь')
        admin=[c for c in self.transport.calls if getattr(c,'chat_id',None)==777]
        self.assertEqual(len(admin),1);self.assertIn('Нужна помощь',admin[0].text)
        self.assertIsNone(await self.state());self.assertTrue(any('передано' in t for t in self.transport.texts()))
        await self.send('/help');await self.send('Ещё вопрос')
        self.assertEqual(len([c for c in self.transport.calls if getattr(c,'chat_id',None)==777]),1)
        await self.send('/help');await self.send('/cancel');self.assertIsNone(await self.state())

    async def test_support_unconfigured_and_failed_delivery_are_safe(self):
        from dataclasses import replace
        self.runtime.settings=replace(self.runtime.settings,admin_id=0)
        await self.send('/help');self.assertIsNone(await self.state())
        self.assertTrue(any('временно недоступна' in t for t in self.transport.texts()))
        self.runtime.settings=replace(self.runtime.settings,admin_id=777)
        self.transport.fail_admin=777
        await self.send('/help');await self.send('Помогите')
        self.assertIsNone(await self.state());self.assertTrue(any('Не удалось доставить' in t for t in self.transport.texts()))

    async def test_inaccessible_stale_and_malformed_callbacks(self):
        await self.click('gen:outline:999');await self.click('card:bad:0:1');await self.click('unknown')
        self.assertTrue(any('устарела' in t or 'недоступна' in t for t in self.transport.texts()))
        before=len(self.transport.calls);await self.click('/start',inaccessible=True)
        calls=self.transport.calls[before:];self.assertEqual(len(calls),1)
        self.assertTrue(calls[0].show_alert)

    async def test_each_upload_format_through_dispatcher(self):
        for category,extensions in (('/add_file',('.txt','.pdf','.docx','.odt')),('/point',('.pdf','.ppt','.pptx','.odp'))):
            for extension in extensions:
                await self.click(category)
                p=self.root/('fixture'+extension);write_fixture(p)
                self.transport.files['fixture']=p.read_bytes()
                message=self.message(document=Document(file_id='fixture',file_unique_id='unique',file_name='lecture'+extension.upper(),file_size=p.stat().st_size))
                await self.dp.feed_update(self.bot,Update(update_id=self.sequence,message=message))
                self.assertIsNone(await self.state(),extension)
        rows,count=await self.runtime.store.lectures(20);self.assertEqual(count,8)

    async def test_concurrent_buttons_one_queued_job_and_cancel(self):
        row=await self.lecture()
        await asyncio.gather(*(self.click(f"gen:outline:{row['id']}") for _ in range(6)))
        active=await self.runtime.store.active_job(20);self.assertIsNotNone(active)
        self.assertEqual(len(self.runtime.queue.pending),1)
        await self.click(f"canceljob:{active['id']}",owner=21)
        self.assertIsNotNone(await self.runtime.store.active_job(20))
        await self.send('/cancel');self.assertIsNone(await self.runtime.store.active_job(20))

    async def test_history_pagination_owner_results_and_export(self):
        result=await self.result(content='Текст результата 😀'*500)
        for i in range(5):await self.lecture(name=f'Лекция {i}.txt')
        await self.click('hist:0');self.assertTrue(any('страница 1' in t for t in self.transport.texts()))
        await self.click('hist:1');self.assertTrue(any('страница 2' in t for t in self.transport.texts()))
        await self.click(f"result:{result['id']}",owner=21)
        before=len([c for c in self.transport.calls if c.__class__.__name__=='SendDocument'])
        await self.click(f"result:{result['id']}")
        after=[c for c in self.transport.calls if c.__class__.__name__=='SendDocument'];self.assertEqual(len(after),before+1)
        data=after[-1].document.data;doc=WordDocument(io.BytesIO(data))
        self.assertTrue(any('Текст результата' in p.text for p in doc.paragraphs))
        self.assertTrue(all(len(t.encode('utf-16-le'))//2<=4096 for t in self.transport.texts()))
        await self.click(f"export:{result['id']}:txt")
        self.assertTrue(self.transport.calls[-1].document.filename.endswith('.txt'))

    async def test_quiz_one_answer_per_question_owner_and_result(self):
        result=await self.result('quiz',content=json.dumps(QUIZ,ensure_ascii=False))
        await self.click(f"quiz:{result['id']}")
        session=await self.runtime.store.quiz_session(20,result['id'])
        await self.click(f"answer:{session['id']}:0:0",owner=21)
        self.assertEqual((await self.runtime.store.session(20,session['id']))['position'],0)
        await asyncio.gather(self.click(f"answer:{session['id']}:0:0"),self.click(f"answer:{session['id']}:0:1"))
        session=await self.runtime.store.session(20,session['id']);self.assertEqual(session['position'],1)
        self.assertEqual(len(json.loads(session['answers'])),1)
        await self.click(f"answer:{session['id']}:1:0")
        self.assertTrue((await self.runtime.store.session(20,session['id']))['finished'])
        self.assertTrue(any('Тест завершён' in t for t in self.transport.texts()))

    async def test_cards_show_answer_navigation_and_owner(self):
        result=await self.result('cards',content=json.dumps(CARDS,ensure_ascii=False))
        await self.click(f"card:{result['id']}:0:0")
        self.assertNotIn(CARDS['cards'][0]['answer'],self.transport.texts()[-1])
        await self.click(f"card:{result['id']}:0:1")
        self.assertIn(CARDS['cards'][0]['answer'],self.transport.texts()[-1])
        await self.click(f"card:{result['id']}:1:0")
        self.assertIn('2 из 2',self.transport.texts()[-1])
        await self.click(f"card:{result['id']}:0:1",owner=21)
        self.assertNotIn(CARDS['cards'][0]['answer'],self.transport.texts()[-1])

    async def test_saved_results_and_quiz_session_open_after_restart(self):
        result = await self.result('quiz', content=json.dumps(QUIZ, ensure_ascii=False))
        await self.click(f"quiz:{result['id']}")
        session = await self.runtime.store.quiz_session(20, result['id'])
        await self.click(f"answer:{session['id']}:0:0")
        await self.runtime.close()
        await self.dp.storage.close()
        self.runtime = Runtime(test_settings(self.root, admin_id=777), self.bot, FakeAI())
        await self.runtime.open(workers=False)
        self.dp = create_dispatcher(self.runtime)
        await self.click('hist:0')
        await self.click(f"result:{result['id']}")
        await self.click(f"quiz:{result['id']}")
        self.assertIn('Вопрос 2 из 2', self.transport.texts()[-1])
        self.assertEqual(self.runtime.ai.calls, [])

    async def test_delete_selected_and_all_does_not_cross_owners(self):
        row=await self.lecture();other=await self.lecture(21)
        await self.click(f"delok:{row['id']}",owner=21)
        self.assertIsNotNone(await self.runtime.store.lecture(20,row['id']))
        await self.click(f"del:{row['id']}");await self.click(f"delok:{row['id']}")
        self.assertIsNone(await self.runtime.store.lecture(20,row['id']))
        self.assertFalse(Path(row['path']).exists())
        await self.lecture();await self.click('deleteall:ask');await self.click('deleteall:yes')
        self.assertEqual((await self.runtime.store.lectures(20))[1],0)
        self.assertIsNotNone(await self.runtime.store.lecture(21,other['id']))

import json

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.database.store import AdmissionError
from app.handlers.common import STALE, identifier
from app.keyboards.inline import keyboard
from app.runtime import Runtime
from app.services.delivery import prefix


async def show_question(message, runtime: Runtime, session: dict) -> None:
    result = await runtime.store.result(session['owner'], session['result_id'])
    if not result:
        await message.answer(STALE)
        return
    questions = json.loads(result['content'])['questions']
    name = await runtime.store.result_name(session['owner'], result['id'])
    if session['finished']:
        await message.answer(f"Тест завершён · {name[:150]}\nРезультат: {session['score']} из {len(questions)}.",
                              reply_markup=keyboard([[('Пройти заново', f"quiz:{result['id']}"), ('Мои лекции', 'hist:0')]]))
        return
    index = session['position']
    question = questions[index]
    options = '\n'.join(f'{i + 1}. {value}' for i, value in enumerate(question['options']))
    await message.answer(f"Файл: {name[:150]}\nВопрос {index + 1} из {len(questions)}\n\n{question['question']}\n\n{options}",
                          reply_markup=keyboard([[(f'Ответ {i + 1}', f"answer:{session['id']}:{index}:{i}")]
                                                 for i in range(len(question['options']))]))


def build_router() -> Router:
    router = Router(name='learning')

    @router.callback_query(F.data.startswith('quiz:'))
    async def quiz(callback: CallbackQuery, runtime: Runtime):
        try:
            session = await runtime.store.quiz_session(callback.from_user.id, identifier(callback.data.split(':')[-1]))
        except AdmissionError as error:
            await callback.message.answer(str(error))
            return
        await show_question(callback.message, runtime, session)

    @router.callback_query(F.data.startswith('answer:'))
    async def answer(callback: CallbackQuery, runtime: Runtime):
        parts = callback.data.split(':')
        if len(parts) != 4:
            await callback.message.answer(STALE)
            return
        try:
            session, question = await runtime.store.answer_quiz(callback.from_user.id, *[identifier(v) for v in parts[1:]])
        except AdmissionError as error:
            await callback.message.answer(str(error))
            return
        option = identifier(parts[3])
        correct = option == question['correct_index']
        await callback.message.answer(('Верно!' if correct else 'Неверно.') +
                                      f"\nПравильный ответ: {question['options'][question['correct_index']]}\n{question['explanation']}")
        await show_question(callback.message, runtime, session)

    @router.callback_query(F.data.startswith('card:'))
    async def card(callback: CallbackQuery, runtime: Runtime):
        parts = callback.data.split(':')
        if len(parts) != 4 or parts[3] not in {'0', '1'}:
            await callback.message.answer(STALE)
            return
        result_id, index = identifier(parts[1]), identifier(parts[2])
        result = await runtime.store.result(callback.from_user.id, result_id)
        if not result or result['mode'] != 'cards':
            await callback.message.answer(STALE)
            return
        cards = json.loads(result['content'])['cards']
        if index >= len(cards):
            await callback.message.answer(STALE)
            return
        item = cards[index]
        name = await runtime.store.result_name(callback.from_user.id, result_id)
        text = f"Файл: {prefix(name, 200)}\nКарточка {index + 1} из {len(cards)}\n\n{item['question']}"
        rows = []
        if parts[3] == '1':
            text += '\n\nОтвет:\n' + item['answer']
        else:
            rows.append([('Показать ответ', f'card:{result_id}:{index}:1')])
        navigation = []
        if index:
            navigation.append(('Назад', f'card:{result_id}:{index - 1}:0'))
        if index + 1 < len(cards):
            navigation.append(('Далее', f'card:{result_id}:{index + 1}:0'))
        if navigation:
            rows.append(navigation)
        rows.append([('Мои лекции', 'hist:0')])
        await callback.message.answer(text, reply_markup=keyboard(rows))
    return router

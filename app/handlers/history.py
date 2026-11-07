from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.database.store import AdmissionError
from app.handlers.common import STALE, identifier
from app.keyboards.inline import Inline_start, keyboard, lecture_keyboard
from app.runtime import Runtime
from app.services.delivery import deliver_result, send_export
from app.services.prompts import MODE_LABELS


def build_router() -> Router:
    router = Router(name='history')

    @router.callback_query(F.data.startswith('hist:'))
    async def history(callback: CallbackQuery, runtime: Runtime):
        page = identifier(callback.data.split(':')[-1])
        rows, count = await runtime.store.lectures(callback.from_user.id, page)
        last_page = max(0, (count - 1) // 5)
        if page > last_page:
            page = last_page
            rows, count = await runtime.store.lectures(callback.from_user.id, page)
        buttons = [[(f"{row['id']}. {row['name'][:55]}", f"lec:{row['id']}")] for row in rows]
        navigation = []
        if page:
            navigation.append(('Назад', f'hist:{page - 1}'))
        if (page + 1) * 5 < count:
            navigation.append(('Далее', f'hist:{page + 1}'))
        if navigation:
            buttons.append(navigation)
        if count:
            buttons.append([('Удалить все мои материалы', 'deleteall:ask')])
        buttons.append([('Главное меню', '/start')])
        await callback.message.answer(f'Мои лекции · страница {page + 1}\nВсего: {count}.' if count else 'Сохранённых лекций пока нет.',
                                      reply_markup=keyboard(buttons))

    @router.callback_query(F.data.startswith('lec:'))
    async def lecture(callback: CallbackQuery, runtime: Runtime):
        row = await runtime.store.lecture(callback.from_user.id, identifier(callback.data.split(':')[-1]))
        if not row:
            await callback.message.answer(STALE)
            return
        await callback.message.answer(f"Файл: {row['name'][:250]}\nРазмер: {row['size'] // 1024} КБ.\nВыберите действие.",
                                      reply_markup=lecture_keyboard(row['id']))
        results = await runtime.store.lecture_results(callback.from_user.id, row['id'])
        if results:
            await callback.message.answer('Сохранённые материалы (повторного запроса к ИИ не будет):',
                reply_markup=keyboard([[(MODE_LABELS[r['mode']], f"result:{r['id']}")] for r in results]))

    @router.callback_query(F.data.startswith('result:'))
    async def result(callback: CallbackQuery, runtime: Runtime):
        result = await runtime.store.result(callback.from_user.id, identifier(callback.data.split(':')[-1]))
        if not result:
            await callback.message.answer(STALE)
            return
        name = await runtime.store.result_name(callback.from_user.id, result['id'])
        await deliver_result(runtime.bot, callback.message.chat.id, result, name, cached=True)

    @router.callback_query(F.data.startswith('export:'))
    async def export(callback: CallbackQuery, runtime: Runtime):
        parts = callback.data.split(':')
        if len(parts) != 3 or parts[2] not in {'docx', 'txt'}:
            await callback.message.answer(STALE)
            return
        result = await runtime.store.result(callback.from_user.id, identifier(parts[1]))
        if not result:
            await callback.message.answer(STALE)
            return
        name = await runtime.store.result_name(callback.from_user.id, result['id'])
        await send_export(runtime.bot, callback.message.chat.id, result, name, parts[2])

    @router.callback_query(F.data.startswith('del:'))
    async def delete_question(callback: CallbackQuery, runtime: Runtime):
        row = await runtime.store.lecture(callback.from_user.id, identifier(callback.data.split(':')[-1]))
        if not row:
            await callback.message.answer(STALE)
            return
        await callback.message.answer(f"Удалить «{row['name'][:250]}» и связанные материалы?",
                                      reply_markup=keyboard([[('Удалить', f"delok:{row['id']}"), ('Отмена', f"lec:{row['id']}")]]))

    @router.callback_query(F.data.startswith('delok:'))
    async def delete(callback: CallbackQuery, runtime: Runtime):
        try:
            count = await runtime.files.delete(callback.from_user.id, identifier(callback.data.split(':')[-1]))
        except AdmissionError as error:
            await callback.message.answer(str(error))
        else:
            await callback.message.answer('Лекция удалена.' if count else STALE, reply_markup=Inline_start)

    @router.callback_query(F.data.startswith('deleteall:'))
    async def delete_all(callback: CallbackQuery, runtime: Runtime):
        if callback.data != 'deleteall:yes':
            await callback.message.answer('Удалить все ваши лекции и сохранённые результаты? Действие нельзя отменить.',
                                          reply_markup=keyboard([[('Удалить всё', 'deleteall:yes'), ('Отмена', 'hist:0')]]))
            return
        try:
            count = await runtime.files.delete(callback.from_user.id)
        except AdmissionError as error:
            await callback.message.answer(str(error))
        else:
            await callback.message.answer(f'Все материалы удалены. Лекций: {count}.', reply_markup=Inline_start)
    return router

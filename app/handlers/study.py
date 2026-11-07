from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.database.store import AdmissionError
from app.handlers.common import STALE, identifier
from app.keyboards.inline import job_keyboard, keyboard
from app.runtime import Runtime
from app.services.delivery import deliver_result

LEGACY_MODES = {'/abs': 'outline', '/short': 'short', '/test': 'quiz', '/card': 'cards'}
STATUS = {'queued': 'В очереди', 'running': 'Создаю материал', 'done': 'Готово', 'error': 'Ошибка',
          'cancelled': 'Отменено', 'interrupted': 'Прервано перезапуском. Действие можно повторить вручную.'}


async def study_callback(callback: CallbackQuery, runtime: Runtime):
    owner = callback.from_user.id
    if callback.data in LEGACY_MODES:
        lecture = await runtime.store.bound_lecture(owner, callback.message.chat.id, callback.message.message_id)
        if not lecture:
            await callback.message.answer(STALE)
            return
        mode, lecture_id = LEGACY_MODES[callback.data], lecture['id']
    else:
        parts = callback.data.split(':')
        if len(parts) != 3:
            await callback.message.answer(STALE)
            return
        mode, lecture_id = parts[1], identifier(parts[2])
        lecture = await runtime.store.lecture(owner, lecture_id)
    try:
        submission = await runtime.queue.submit(owner, lecture_id, mode, callback.message.chat.id)
    except AdmissionError as error:
        await callback.message.answer(str(error))
        return
    if submission.kind == 'cached':
        await deliver_result(runtime.bot, callback.message.chat.id, submission.record, f"{lecture['name']} (лекция №{lecture['id']})", cached=True)
    elif submission.kind == 'active':
        await callback.message.answer('У вас уже есть незавершённое задание. Повторный платный запрос не создан.',
                                      reply_markup=job_keyboard(submission.record['id']))


def build_router() -> Router:
    router = Router(name='study')
    router.callback_query.register(study_callback, F.data.startswith('gen:') | F.data.in_(set(LEGACY_MODES)))

    @router.callback_query(F.data.startswith('canceljob:'))
    async def cancel_job(callback: CallbackQuery, runtime: Runtime):
        cancelled = await runtime.queue.cancel(callback.from_user.id, identifier(callback.data.split(':')[-1]))
        await callback.message.answer('Ожидающее задание отменено.' if cancelled else
                                      'Задание уже завершено, начато или недоступно. Начатую генерацию остановить нельзя.')

    @router.callback_query(F.data.startswith('job:'))
    async def job_status(callback: CallbackQuery, runtime: Runtime):
        job = await runtime.store.job(callback.from_user.id, identifier(callback.data.split(':')[-1]))
        if not job:
            await callback.message.answer(STALE)
            return
        await callback.message.answer(f"Файл: {job['name'][:250]}\n{STATUS[job['status']]}",
                                      reply_markup=keyboard([[('Открыть лекцию', f"lec:{job['lecture_id']}")]]))
    return router

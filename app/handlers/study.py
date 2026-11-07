from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery

from app.services import lecture_storage
from app.services.study_ai import (
    SUPPORTED_EXTENSIONS, SUPPORTED_FORMATS_MESSAGE, StudyAIError, generate_study_material,
)

study = Router()
STALE = 'Откройте кнопки под актуальной лекцией либо загрузите файл заново.'


@study.callback_query(F.data.in_({'/abs', '/short'}))
async def study_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not callback.message:
        return
    path = None
    async with lecture_storage.locks[callback.from_user.id]:
        data = await state.get_data()
        if (data.get('lecture_user_id') != callback.from_user.id
                or data.get('lecture_message_id') != callback.message.message_id
                or data.get('lecture_chat_id') != callback.message.chat.id
                or not data.get('lecture_path')):
            await callback.message.answer(STALE)
            return
        path = data['lecture_path']
        if not Path(path).is_file():
            await callback.message.answer(STALE)
            return
        if Path(path).suffix.lower() not in SUPPORTED_EXTENSIONS:
            await callback.message.answer(SUPPORTED_FORMATS_MESSAGE)
            return
        lecture_storage.retain(path)
    try:
        await callback.message.answer('Готовлю материал по лекции…')
        mode = 'outline' if callback.data == '/abs' else 'short'
        try:
            result = await generate_study_material(path, mode)
        except StudyAIError as error:
            await callback.message.answer(str(error), parse_mode=None)
            return
        # Telegram counts UTF-16 code units, including two units for emoji.
        if len(result.encode('utf-16-le')) // 2 <= 4096:
            await callback.message.answer(result, parse_mode=None)
        else:
            await callback.message.answer_document(
                BufferedInputFile(result.encode('utf-8'), filename=f'{mode}.txt'),
                caption='Материал по лекции «' + data.get('lecture_name', '')[:200] + '»',
                parse_mode=None,
            )
    finally:
        lecture_storage.release(path)

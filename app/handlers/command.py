import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from app.config import PROJECT_ROOT
from app.keyboards.inline import Inline_in_start, Inline_start
from app.runtime import Runtime
from app.services.delivery import utf16_length
from app.states.upload import UploadState

log = logging.getLogger(__name__)
MENU_TEXT = ('Здравствуйте! Я помогу разобрать учебный материал.\n\n'
             'Загрузите лекцию или откройте «Мои лекции», затем выберите конспект, '
             'краткую выжимку, тест или карточки.\n\n'
             'Для генерации материал передаётся в GigaChat. Срок хранения и удаление — в разделе «Хранение и удаление данных».')


async def menu(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.set_data({})
    await message.answer_photo(FSInputFile(PROJECT_ROOT / 'assets/bot_avatar.png'),
                                caption=MENU_TEXT, reply_markup=Inline_start)


async def cancel_dialog(message: Message, state: FSMContext, runtime: Runtime, owner: int) -> None:
    await runtime.files.cancel_upload(owner)
    await state.set_state(None)
    await state.set_data({})
    cancelled = await runtime.queue.cancel(owner)
    active = await runtime.store.active_job(owner)
    text = 'Ожидающее задание отменено.' if cancelled else 'Текущий диалог отменён.'
    if active:
        text += '\nУже начатую генерацию остановить нельзя. Результат появится в истории.'
    await message.answer(text + '\nСохранённые лекции доступны в меню.', reply_markup=Inline_start)


async def begin_support(message: Message, state: FSMContext, runtime: Runtime) -> None:
    if not runtime.settings.support_enabled or not runtime.settings.admin_id:
        await state.set_state(None)
        await message.answer('Поддержка временно недоступна.', reply_markup=Inline_start)
        return
    await state.set_state(UploadState.waiting_for_feedback)
    await message.answer('Напишите обращение одним текстовым сообщением (до 3000 символов).\nОтмена: /cancel.',
                         reply_markup=Inline_in_start)


async def feedback(message: Message, bot: Bot, state: FSMContext, runtime: Runtime):
    if not message.text or message.text.startswith('/'):
        await message.answer('Отправьте текст обращения или используйте /cancel.')
        return
    if not runtime.settings.support_enabled or not runtime.settings.admin_id:
        await state.set_state(None)
        await message.answer('Поддержка временно недоступна.', reply_markup=Inline_start)
        return
    if utf16_length(message.text) > 3000:
        await message.answer('Обращение слишком длинное. Сократите его до 3000 символов.')
        return
    if not await runtime.store.reserve_support(message.from_user.id, runtime.settings.support_cooldown):
        await state.set_state(None)
        await message.answer('Вы недавно отправляли обращение. Попробуйте позже.', reply_markup=Inline_start)
        return
    try:
        await bot.send_message(runtime.settings.admin_id,
                               f'Обращение пользователя {message.from_user.id}:\n\n{message.text}', parse_mode=None)
    except TelegramAPIError as error:
        log.warning('support_delivery_failed owner=%s type=%s', message.from_user.id, type(error).__name__)
        await message.answer('Не удалось доставить обращение. Поддержка временно недоступна.', reply_markup=Inline_start)
    else:
        await message.answer('Обращение передано в поддержку. Спасибо!', reply_markup=Inline_start)
    finally:
        await state.set_state(None)
        await state.set_data({})


def build_router() -> Router:
    router = Router(name='commands')

    @router.message(Command('start'))
    async def start_command(message: Message, state: FSMContext, runtime: Runtime):
        await runtime.files.cancel_upload(message.from_user.id)
        await menu(message, state)

    @router.message(Command('help'))
    async def help_command(message: Message, state: FSMContext, runtime: Runtime):
        await runtime.files.cancel_upload(message.from_user.id)
        await begin_support(message, state, runtime)

    @router.message(Command('cancel'))
    async def cancel_command(message: Message, state: FSMContext, runtime: Runtime):
        await cancel_dialog(message, state, runtime, message.from_user.id)

    @router.callback_query(F.data.in_({'/start', '/continue'}))
    async def start_button(callback: CallbackQuery, state: FSMContext, runtime: Runtime):
        await runtime.files.cancel_upload(callback.from_user.id)
        await menu(callback.message, state)

    @router.callback_query(F.data == '/cancel')
    async def cancel_button(callback: CallbackQuery, state: FSMContext, runtime: Runtime):
        await cancel_dialog(callback.message, state, runtime, callback.from_user.id)

    @router.callback_query(F.data == '/help')
    async def help_button(callback: CallbackQuery, state: FSMContext, runtime: Runtime):
        await runtime.files.cancel_upload(callback.from_user.id)
        await begin_support(callback.message, state, runtime)

    @router.callback_query(F.data.in_({'/add_file', '/point'}))
    async def upload_button(callback: CallbackQuery, state: FSMContext, runtime: Runtime):
        await runtime.files.cancel_upload(callback.from_user.id)
        presentation = callback.data == '/point'
        await state.set_state(UploadState.waiting_for_presentation if presentation else UploadState.waiting_for_file)
        await state.set_data({})
        formats = '.pdf, .ppt, .pptx, .odp' if presentation else '.txt, .pdf, .odt, .docx'
        await callback.message.answer_photo(
            FSInputFile(PROJECT_ROOT / 'assets/waiting.png'),
            caption=f'Ожидаю {"презентацию" if presentation else "лекцию"}.\nФорматы: {formats}.\n'
                    f'Лимит: {runtime.settings.max_upload_bytes // 1024 // 1024} МБ.\nОтмена: /cancel.', reply_markup=Inline_in_start)

    @router.callback_query(F.data == '/skills')
    async def skills_button(callback: CallbackQuery):
        await callback.message.answer_photo(FSInputFile(PROJECT_ROOT / 'assets/Telling_bot.png'),
            caption='Создаю подробный конспект, краткую выжимку, тест и карточки по загруженной лекции.\n\n'
                    'Сохранённые лекции и результаты доступны в «Мои лекции». Проверяйте ответы ИИ по исходному материалу.',
            reply_markup=Inline_start)

    @router.callback_query(F.data == 'privacy')
    async def privacy(callback: CallbackQuery, runtime: Runtime):
        await callback.message.answer(
            f'Для создания материалов файл или извлечённый текст передаётся в GigaChat.\n'
            f'Лекции и результаты хранятся у бота до {runtime.settings.retention_days} дней. '
            'Файл, занятый заданием, удаляется после его завершения.\n'
            'Отдельную лекцию можно удалить в «Мои лекции». Там же доступно удаление всех ваших материалов.\n'
            'История доступна только владельцу. Не загружайте чужие персональные данные без разрешения.\n'
            'Для DOCX, ODT и ODP извлекается текст; изображения и встроенные объекты формул не распознаются.',
            reply_markup=Inline_start)

    router.message.register(feedback, UploadState.waiting_for_feedback)
    return router

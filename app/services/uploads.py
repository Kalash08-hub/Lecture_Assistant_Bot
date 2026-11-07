import asyncio
import logging
from pathlib import Path

from aiogram.types import FSInputFile

from app.config import PROJECT_ROOT
from app.database.store import AdmissionError
from app.keyboards.inline import lecture_keyboard
from app.runtime import Runtime
from app.services.archive_text import DocumentError

log = logging.getLogger(__name__)


async def receive_upload(message, bot, state, allowed: set[str], kind: str, runtime: Runtime) -> None:
    document = message.document
    if not document:
        await message.answer(f'Отправьте {kind} файлом или используйте /cancel.')
        return
    name = document.file_name or ''
    if Path(name).suffix.lower() not in allowed:
        await message.answer('Поддерживаемые форматы: ' + ', '.join(sorted(allowed)))
        return
    row = None
    try:
        row = await runtime.files.save(message.from_user.id, name, document, bot)
        buttons = await message.answer_photo(
            photo=FSInputFile(PROJECT_ROOT / 'assets/accepted.png'),
            caption=f"Файл «{name[:250]}» сохранён.\nВыберите действие ниже.\n\n"
                    f"Для генерации материал передаётся в GigaChat. Хранение — {runtime.settings.retention_days} дней. "
                    'Удаление: «Мои лекции».',
            reply_markup=lecture_keyboard(row['id']), parse_mode=None)
        await runtime.store.bind(message.from_user.id, message.chat.id, buttons.message_id, row['id'])
        await state.set_state(None)
        await state.set_data({})
    except (DocumentError, AdmissionError) as error:
        await message.answer(str(error), parse_mode=None)
    except asyncio.CancelledError:
        if row:
            await runtime.files.delete(message.from_user.id, row['id'])
        raise
    except Exception as error:
        if row:
            try:
                await runtime.files.delete(message.from_user.id, row['id'])
            except AdmissionError:
                pass
        log.warning('upload_error owner=%s type=%s', message.from_user.id, type(error).__name__)
        await message.answer('Не удалось сохранить лекцию. Предыдущие файлы сохранены; повторите загрузку.')

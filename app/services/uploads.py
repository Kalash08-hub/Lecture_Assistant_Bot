from pathlib import Path
from uuid import uuid4

from aiogram.types import FSInputFile

from app.config import PROJECT_ROOT, UPLOAD_DIR
from app.keyboards.inline import Inline_processing_file
from app.services import lecture_storage


async def receive_upload(message, bot, state, allowed, kind):
    document = message.document
    if not document:
        await message.answer(f'❌ Отправьте {kind} файлом.')
        return
    original_name = document.file_name or ''
    extension = Path(original_name).suffix.lower()
    if extension not in allowed:
        await message.answer('❌ Поддерживаемые форматы: ' + ', '.join(sorted(allowed)))
        return

    async with lecture_storage.locks[message.from_user.id]:
        data = await state.get_data()
        directory = UPLOAD_DIR / str(message.from_user.id)
        path = directory / f'{uuid4()}{extension}'
        try:
            directory.mkdir(parents=True, exist_ok=True)
            remote = await bot.get_file(document.file_id)
            await bot.download_file(remote.file_path, destination=path)
            buttons = await message.answer_photo(
                photo=FSInputFile(PROJECT_ROOT / 'assets/accepted.png'),
                caption=f'✅ Файл {original_name[:250]} принят.\n\nВыберите действие ниже 👇',
                reply_markup=Inline_processing_file,
                parse_mode=None,
            )
        except Exception:
            lecture_storage.retire(str(path))
            await message.answer('Не удалось сохранить лекцию. Попробуйте загрузить файл ещё раз.')
            return
        # Only commit after download and delivery of the new buttons succeed.
        await state.update_data(
            lecture_path=str(path), lecture_name=original_name,
            lecture_message_id=buttons.message_id, lecture_chat_id=message.chat.id,
            lecture_user_id=message.from_user.id,
        )
        await state.set_state(None)
        lecture_storage.retire(data.get('lecture_path'))

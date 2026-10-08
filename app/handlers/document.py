from aiogram import Router, Bot
from aiogram.types import Message, InputMediaPhoto, FSInputFile
from aiogram.fsm.context import FSMContext
from app.states.upload import UploadState
from pathlib import Path
from uuid import uuid4
from app.keyboards.inline import Inline_processing_file

document = Router()

UPLOAD_DIR = Path("files")
UPLOAD_DIR.mkdir(exist_ok=True)
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".odt", ".txt"}

@document.message(UploadState.waiting_for_file)
async def receive_pdf(message: Message, bot: Bot, state: FSMContext):
    if not message.document:
        await message.answer("❌ Пожалуйста, отправьте текстовый-файл.")
        return

    data = await state.get_data()
    bot_message_id = data.get("bot_message_id")

    document = message.document
    original_file_name = document.file_name
    extension = Path(document.file_name).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        await message.answer(
            "❌ Данный формат файла не поддерживается.\n\n"
            "Отправьте файл формата:\n"
            "-> .pdf\n"
            "-> .docx\n"
            "-> .odt\n"
            "-> .txt"
        )
        return

    file = await bot.get_file(document.file_id)

    file_name = f"{uuid4()}{extension}"

    file_path = UPLOAD_DIR / file_name

    await bot.download_file(
        file.file_path,
        destination=file_path
    )

    await bot.edit_message_media(
        chat_id=message.chat.id,
        message_id=bot_message_id,
        media=InputMediaPhoto(
            media=FSInputFile("assets/accepted.png"),
            caption=f"✅ файл {original_file_name}\n"
                    "принят успешно.\n\n"
                    "Начинаю обработку материала 🔄"
        )
    )

    await message.answer_photo(
        photo=FSInputFile("assets/file_accepted.png"),
        caption="Отлично 👍\n\n"
                "Теперь скажите, что мне\n"
                "сделать с файлом\n\n"
                "Выберите действие ниже 👇",
        reply_markup=Inline_processing_file
    )

    await state.clear()
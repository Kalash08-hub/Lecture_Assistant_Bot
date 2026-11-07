from aiogram import Bot, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.services.uploads import receive_upload
from app.states.upload import UploadState

document = Router()
ALLOWED_EXTENSIONS = {'.pdf', '.docx', '.odt', '.txt'}


@document.message(UploadState.waiting_for_file)
async def receive_pdf(message: Message, bot: Bot, state: FSMContext):
    await receive_upload(message, bot, state, ALLOWED_EXTENSIONS, 'лекцию')

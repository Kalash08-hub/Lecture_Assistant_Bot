from aiogram import Bot, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.services.uploads import receive_upload
from app.states.upload import UploadState

presentation = Router()
ALLOWED_EXTENSIONS = {'.pdf', '.ppt', '.pptx', '.odp'}


@presentation.message(UploadState.waiting_for_presentation)
async def receive_presentation(message: Message, bot: Bot, state: FSMContext):
    await receive_upload(message, bot, state, ALLOWED_EXTENSIONS, 'презентацию')

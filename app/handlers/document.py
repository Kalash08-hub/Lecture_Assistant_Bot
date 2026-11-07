from aiogram import Bot, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.runtime import Runtime
from app.services.uploads import receive_upload
from app.states.upload import UploadState

ALLOWED_EXTENSIONS = {'.pdf', '.docx', '.odt', '.txt'}


async def receive_pdf(message: Message, bot: Bot, state: FSMContext, runtime: Runtime):
    await receive_upload(message, bot, state, ALLOWED_EXTENSIONS, 'лекцию', runtime)


def build_router() -> Router:
    router = Router(name='documents')
    router.message.register(receive_pdf, UploadState.waiting_for_file)
    return router

from aiogram import Bot, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.runtime import Runtime
from app.services.uploads import receive_upload
from app.states.upload import UploadState

ALLOWED_EXTENSIONS = {'.pdf', '.ppt', '.pptx', '.odp'}


async def receive_presentation(message: Message, bot: Bot, state: FSMContext, runtime: Runtime):
    await receive_upload(message, bot, state, ALLOWED_EXTENSIONS, 'презентацию', runtime)


def build_router() -> Router:
    router = Router(name='presentations')
    router.message.register(receive_presentation, UploadState.waiting_for_presentation)
    return router

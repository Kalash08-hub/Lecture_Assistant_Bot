import asyncio
from aiogram import Bot, Dispatcher
from aiogram.fsm.strategy import FSMStrategy
from app.config import BOT_TOKEN
from app.network.proxy import session
from app.handlers.command import callback_start, callback_skills, unknown, callback_back, state, start, help
from app.handlers.document import document
from app.handlers.presentation import presentation
from app.handlers.study import study

async def main():
    bot = Bot(token=BOT_TOKEN, session=session)
    dp = Dispatcher(fsm_strategy=FSMStrategy.GLOBAL_USER)

    dp.include_router(start)
    dp.include_router(help)
    dp.include_router(callback_start)
    dp.include_router(callback_skills)
    dp.include_router(state)
    dp.include_router(document)
    dp.include_router(presentation)
    dp.include_router(study)
    dp.include_router(callback_back)
    dp.include_router(unknown)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

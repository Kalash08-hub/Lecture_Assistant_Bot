import asyncio
from aiogram import Bot, Dispatcher
from app.config import BOT_TOKEN
from app.network.proxy import session
from app.handlers.command import callback_start, callback_skills, \
    unknown, callback_back, state, start, help
from app.handlers.document import document

async def main():
    bot = Bot(token=BOT_TOKEN, session=session)
    dp = Dispatcher()

    dp.include_router(start)
    dp.include_router(help)
    dp.include_router(callback_start)
    dp.include_router(callback_skills)
    dp.include_router(state)
    dp.include_router(document)
    dp.include_router(callback_back)
    dp.include_router(unknown)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
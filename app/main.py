import asyncio
import fcntl
import logging
import sqlite3
import sys
from contextlib import contextmanager

from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.strategy import FSMStrategy
from aiogram.types import BotCommand

from app.config import Settings
from app.handlers import command, document, history, learning, presentation, study
from app.handlers.common import SafeUpdates, STALE
from app.runtime import Runtime
from app.keyboards.inline import Inline_start


def create_dispatcher(runtime: Runtime) -> Dispatcher:
    dispatcher = Dispatcher(fsm_strategy=FSMStrategy.GLOBAL_USER, runtime=runtime)
    dispatcher.message.outer_middleware(SafeUpdates())
    dispatcher.callback_query.outer_middleware(SafeUpdates())
    for module in (command, study, history, learning, document, presentation):
        dispatcher.include_router(module.build_router())

    fallback = Router(name='fallback')

    @fallback.callback_query()
    async def unknown_callback(callback):
        await callback.message.answer(STALE, reply_markup=Inline_start)

    @fallback.message()
    async def unknown_message(message):
        await message.answer('Выберите действие в меню. Для отмены используйте /cancel.', reply_markup=Inline_start)
    dispatcher.include_router(fallback)
    return dispatcher


@contextmanager
def single_instance(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + '.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Бот уже запущен с этой DATABASE_PATH. Остановите вторую копию.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def main() -> None:
    if sys.version_info[:2] != (3, 12):
        raise ValueError('Используйте Python 3.12 с OpenSSL (см. README).')
    settings = Settings.from_env()
    settings.validate()
    logging.basicConfig(level=settings.log_level, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    # Avoid DEBUG logging in HTTP libraries; no bodies/authorization in our logs.
    for name in ('aiohttp', 'aiosqlite'):
        logging.getLogger(name).setLevel(logging.WARNING)
    with single_instance(settings.database_path):
        bot = Bot(settings.bot_token, session=AiohttpSession(proxy=settings.proxy_url or None))
        runtime = Runtime(settings, bot)
        dispatcher = None
        try:
            try:
                await runtime.open()
            except (OSError, sqlite3.Error) as error:
                logging.getLogger(__name__).error('startup_storage_error type=%s', type(error).__name__)
                raise ValueError('DATABASE_PATH/UPLOAD_DIR: проверьте доступ к хранилищу и свободное место') from None
            dispatcher = create_dispatcher(runtime)
            await bot.set_my_commands([BotCommand(command='start', description='Главное меню'),
                                       BotCommand(command='help', description='Поддержка'),
                                       BotCommand(command='cancel', description='Отменить диалог или ожидание')])
            await dispatcher.start_polling(bot, close_bot_session=False)
        finally:
            await runtime.close()
            if dispatcher:
                await dispatcher.storage.close()
            await bot.session.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except ValueError as error:
        raise SystemExit(f'Ошибка настройки: {error}') from None

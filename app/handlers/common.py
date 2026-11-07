import asyncio
import logging
from contextlib import suppress

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

log = logging.getLogger(__name__)
STALE = 'Кнопка устарела или материал удалён. Откройте «Мои лекции» либо загрузите файл заново.'


class InvalidCallback(ValueError):
    pass


def identifier(value: str) -> int:
    if not value.isdecimal() or len(value) > 18:
        raise InvalidCallback
    return int(value)


class SafeUpdates(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery):
            if not isinstance(event.message, Message):
                with suppress(TelegramAPIError):
                    await event.answer('Сообщение недоступно. Откройте /start.', show_alert=True)
                return
            with suppress(TelegramAPIError):
                await event.answer()
        runtime = data.get('runtime')
        if runtime and runtime.closing:
            message = event.message if isinstance(event, CallbackQuery) else event
            with suppress(TelegramAPIError):
                await message.answer('Бот завершает работу. Повторите действие позже.')
            return
        task = asyncio.current_task()
        if runtime:
            runtime.updates.add(task)
        try:
            return await handler(event, data)
        except InvalidCallback:
            message = event.message if isinstance(event, CallbackQuery) else event
            with suppress(TelegramAPIError):
                await message.answer(STALE)
        except TelegramAPIError as error:
            log.warning('telegram_delivery_error type=%s', type(error).__name__)
        except Exception as error:
            log.error('update_error type=%s', type(error).__name__)
            message = event.message if isinstance(event, CallbackQuery) else event
            if isinstance(message, Message):
                with suppress(TelegramAPIError):
                    await message.answer('Не удалось выполнить действие. Попробуйте позже или откройте /help.')
        finally:
            if runtime:
                runtime.updates.discard(task)

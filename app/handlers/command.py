from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, FSInputFile, InputMediaPhoto
from aiogram.fsm.context import FSMContext
from app.keyboards.inline import Inline_unknown, Inline_start, Inline_skills, Inline_in_start
from app.states.upload import UploadState

start = Router()
unknown = Router()
unknown_continue = Router()
callback_start = Router()
callback_skills = Router()
callback_back = Router()
state = Router()
help = Router()

@start.message(CommandStart())
async def handle_start(message: Message):
    await message.answer_photo(
        photo=FSInputFile("assets/bot_avatar.png"),
        caption="👋 Привет! Я твой учебный помощник\n\n"
                "Я помогу превратить учебные материалы\n"
                "в удобный конспект.\n\n"
                "⚡️ Быстро\n"
                "⭐️ Удобно\n"
                "📝 Кратко и по делу\n\n"
                "💡 Используйте кнопки ниже для управления",
        reply_markup=Inline_start
        )
    await message.answer()

@unknown.message()
async def unknown_command(message: Message):
    await message.answer(
        "<b>🤖 Я пока не умею отвечать на такие сообщения</b>\n\n"
        "<b>❓ У вас вопрос или возникли сложности?</b>\n"
        "Свяжитесь с нашей поддержкой — мы поможем как можно скорее.\n\n"
        "💡 Используйте кнопки ниже",
        parse_mode="HTML",
        reply_markup=Inline_unknown,
    )

@unknown.callback_query(F.data == "/continue")
async def close_unknown(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer()

@callback_skills.callback_query(F.data == "/skills")
async def skills_button(callback: CallbackQuery):
    await callback.message.edit_media(
        media = InputMediaPhoto(
            media = FSInputFile("assets/telling_bot.png"),
            caption = "👋 Привет, если ты до сих пор не знаешь,\n"
                      "что я могу для тебя сделать,\n"
                      "значит ты в правильном месте.\n\n"
                      "Сейчас я кратко расскажу тебе о своих возможностях:\n\n"
                      "1. Отправь мне лекционные материалы:\n\n"
                      "👉 Текстовый файл\n"
                      "👉 Презентацию\n\n"
                      "2. Выбери, что я должен сделать:\n\n"
                      "📝 Конспект\n"
                      "🤏 Краткую выжимку\n"
                      "❓ Тест по теме\n"
                      "🧠 Карточки для запоминания\n\n"
                      "После этого я проанализирую твой запрос и выдам тебе то, о чем ты меня просил.\n\n"
                      "Для взаимодействия со мной используй кнопки под сообщениями 👇"
        ),
        reply_markup=Inline_skills
    )
    await callback.answer()

@callback_start.callback_query(F.data == "/start")
async def start_button(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_media(
        media = InputMediaPhoto(
            media=FSInputFile("assets/bot_avatar.png"),
            caption="👋 Привет! Я твой учебный помощник\n\n"
                "Я помогу превратить учебные материалы\n"
                "в удобный конспект.\n\n"
                "⚡️ Быстро\n"
                "⭐️ Удобно\n"
                "📝 Кратко и по делу\n\n"
                "💡 Используй кнопки ниже для управления"
        ),
    reply_markup=Inline_start
    )
    await callback.answer()

@state.callback_query(F.data == "/add_file")
async def add_file_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UploadState.waiting_for_file)
    await state.update_data(
        bot_message_id=callback.message.message_id
    )
    await callback.message.edit_media(
        media = InputMediaPhoto(
            media = FSInputFile("assets/waiting.png"),
            caption = "Ожиданию ваш файла...\n\n"
            "Отправьте файл в этот чат\n"
            "и я начну обрабатывать его"
        ),
    )
    await callback.message.edit_reply_markup(
        reply_markup=Inline_in_start
    )
    await state.set_state(UploadState.waiting_for_file)
    await callback.answer()

@help.callback_query(F.data == "/help")
async def help_button(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UploadState.waiting_for_feedback)
    await callback.message.edit_media(
        media = InputMediaPhoto(
            media = FSInputFile("assets/help.png"),
            caption = "Напишите свой вопрос..."
        ),
    )
    await state.set_state(UploadState.waiting_for_feedback)
    await callback.answer()
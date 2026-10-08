from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

Inline_start = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Добавить файл", callback_data="/add_file")],
        [InlineKeyboardButton(text="Добавить презентацию", callback_data="/point")],
        [InlineKeyboardButton(text="Возможности бота", callback_data="/skills")],
        [InlineKeyboardButton(text="Поддержка", callback_data="/help")]
    ]
)

Inline_unknown = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Продолжить работу", callback_data="/continue")],
        [InlineKeyboardButton(text="Помощь", callback_data="/help")]
    ]
)

Inline_skills = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Начать работу", callback_data="/start")],
        [InlineKeyboardButton(text="Поддержка", callback_data="/help")]
    ]
)

Inline_in_start = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="/start")]
    ]
)

Inline_processing_file = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Конспект", callback_data="/abs")],
        [InlineKeyboardButton(text="Краткая выжимка", callback_data="/short")],
        [InlineKeyboardButton(text="Тест по теме", callback_data="/test")],
        [InlineKeyboardButton(text="Карточки", callback_data="/card")]
    ]
)
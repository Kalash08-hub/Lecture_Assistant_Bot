from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows
    ])


Inline_start = keyboard([
    [('Добавить файл', '/add_file')], [('Добавить презентацию', '/point')],
    [('Мои лекции', 'hist:0')], [('Возможности бота', '/skills')],
    [('Поддержка', '/help')], [('Хранение и удаление данных', 'privacy')],
])
Inline_unknown = Inline_start
Inline_skills = Inline_start
Inline_in_start = keyboard([[('Отмена', '/cancel')], [('Главное меню', '/start')]])
Inline_processing_file = keyboard([
    [('Конспект', '/abs')], [('Краткая выжимка', '/short')],
    [('Тест по теме', '/test')], [('Карточки', '/card')],
])


def lecture_keyboard(lecture_id: int) -> InlineKeyboardMarkup:
    return keyboard([
        [('Конспект', f'gen:outline:{lecture_id}'), ('Краткая выжимка', f'gen:short:{lecture_id}')],
        [('Тест по теме', f'gen:quiz:{lecture_id}'), ('Карточки', f'gen:cards:{lecture_id}')],
        [('Сохранённые результаты', f'lec:{lecture_id}')],
        [('Удалить лекцию', f'del:{lecture_id}'), ('Мои лекции', 'hist:0')],
    ])


def job_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return keyboard([[('Статус', f'job:{job_id}'), ('Отменить ожидание', f'canceljob:{job_id}')], [('Мои лекции', 'hist:0')]])

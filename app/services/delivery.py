"""Telegram-sized previews and Unicode DOCX/TXT exports."""
import asyncio
import io
import json
import re

from aiogram.types import BufferedInputFile
from docx import Document
from docx.shared import Pt

from app.database.store import Record
from app.keyboards.inline import keyboard
from app.services.prompts import MODE_LABELS


def utf16_length(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def prefix(text: str, limit: int) -> str:
    return text.encode('utf-16-le')[:limit * 2].decode('utf-16-le', errors='ignore')


def export_text(result: Record) -> str:
    if result['mode'] == 'quiz':
        questions = json.loads(result['content'])['questions']
        return '\n\n'.join(f"{i}. {q['question']}\n" + '\n'.join(f"{j + 1}) {o}" for j, o in enumerate(q['options']))
                            + f"\nВерный ответ: {q['correct_index'] + 1}\n{q['explanation']}" for i, q in enumerate(questions, 1))
    if result['mode'] == 'cards':
        return '\n\n'.join(f"{i}. {c['question']}\n{c['answer']}" for i, c in enumerate(json.loads(result['content'])['cards'], 1))
    return result['content']


def docx_bytes(title: str, text: str) -> bytes:
    document = Document()
    style = document.styles['Normal']
    style.font.name = 'Arial'
    style.font.size = Pt(11)
    document.add_heading(title, 0)
    for line in text.splitlines():
        # Strip only basic heading markup emitted despite plain-text instructions.
        if line.startswith('#'):
            document.add_heading(line.lstrip('# ').replace('**', ''), 2)
        else:
            document.add_paragraph(line.replace('**', ''))
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def filename(name: str, mode: str, extension: str) -> str:
    stem = re.sub(r'[^\w -]+', '_', name.rsplit('.', 1)[0], flags=re.UNICODE).strip()[:60] or 'Лекция'
    return f'{stem}_{MODE_LABELS[mode]}.{extension}'


def result_keyboard(result: Record):
    rows = [[('Скачать DOCX', f"export:{result['id']}:docx"), ('Скачать TXT', f"export:{result['id']}:txt")]]
    if result['mode'] == 'quiz':
        rows.insert(0, [('Пройти тест', f"quiz:{result['id']}")])
    elif result['mode'] == 'cards':
        rows.insert(0, [('Открыть карточки', f"card:{result['id']}:0:0")])
    rows.append([('Мои лекции', 'hist:0')])
    return keyboard(rows)


async def send_export(bot, chat_id: int, result: Record, name: str, extension: str) -> None:
    text = export_text(result)
    title = f"{MODE_LABELS[result['mode']]} — {name}"
    if extension == 'docx':
        try:
            data = await asyncio.to_thread(docx_bytes, title, text)
        except Exception:
            extension, data = 'txt', text.encode('utf-8')
    else:
        extension, data = 'txt', text.encode('utf-8')
    await bot.send_document(chat_id, BufferedInputFile(data, filename=filename(name, result['mode'], extension)),
                            caption=prefix(title, 900), parse_mode=None)


async def deliver_result(bot, chat_id: int, result: Record, name: str, cached: bool = False) -> None:
    heading = f"{'Сохранённый материал' if cached else 'Готово'}: {MODE_LABELS[result['mode']]}\nФайл: {name[:250]}\n\n"
    if result['mode'] in {'quiz', 'cards'}:
        data = json.loads(result['content'])
        size = len(data['questions' if result['mode'] == 'quiz' else 'cards'])
        await bot.send_message(chat_id, heading + f'Количество: {size}. Откройте набор кнопкой ниже.',
                               reply_markup=result_keyboard(result), parse_mode=None)
        return
    text = heading + result['content']
    if utf16_length(text) <= 4096:
        await bot.send_message(chat_id, text, reply_markup=result_keyboard(result), parse_mode=None)
    else:
        preview = heading + prefix(result['content'], 900) + '\n\nПолный материал — в приложенном документе.'
        await bot.send_message(chat_id, preview, reply_markup=result_keyboard(result), parse_mode=None)
        await send_export(bot, chat_id, result, name, 'docx')

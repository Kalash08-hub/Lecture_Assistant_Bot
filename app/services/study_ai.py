"""Prepare documents, call GigaChat and validate the resulting study material."""
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

from app.config import Settings
from app.services.archive_text import DocumentError
from app.services.content import SUPPORTED_EXTENSIONS, SUPPORTED_FORMATS_MESSAGE, prepare_content
from app.services.gigachat import GigaChatClient, StudyAIError
from app.services.prompts import PROMPTS, SYSTEM


class Question(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    question: str = Field(min_length=1, max_length=700)
    options: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(min_length=2, max_length=5)
    correct_index: StrictInt
    explanation: str = Field(min_length=1, max_length=1000)

    @model_validator(mode='after')
    def valid_options(self):
        if not 0 <= self.correct_index < len(self.options):
            raise ValueError('Invalid correct index')
        if len({value.strip().casefold() for value in self.options}) != len(self.options):
            raise ValueError('Duplicate options')
        if sum(len(v.encode('utf-16-le')) // 2 for v in [self.question, *self.options]) > 2800:
            raise ValueError('Question exceeds Telegram size')
        if not self.question.strip() or not self.explanation.strip() or any(not v.strip() for v in self.options):
            raise ValueError('Empty fields')
        return self


class Quiz(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    questions: list[Question] = Field(min_length=1, max_length=8)


class Card(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    question: str = Field(min_length=1, max_length=700)
    answer: str = Field(min_length=1, max_length=1500)

    @model_validator(mode='after')
    def nonempty(self):
        if len((self.question + self.answer).encode('utf-16-le')) // 2 > 3200:
            raise ValueError('Card exceeds Telegram size')
        if not self.question.strip() or not self.answer.strip():
            raise ValueError('Empty card')
        return self


class Cards(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    cards: list[Card] = Field(min_length=1, max_length=12)


@dataclass(frozen=True)
class GeneratedMaterial:
    content: str
    model: str
    usage: dict | None = None


def validate_result(text: str, mode: str) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > 200_000:
        raise StudyAIError('Модель вернула пустой или слишком большой ответ.', 'response')
    try:
        text.encode('utf-8')
    except UnicodeError:
        raise StudyAIError('Модель вернула некорректную кодировку.', 'response') from None
    if mode in {'quiz', 'cards'}:
        # Some models wrap otherwise valid JSON; accept a single fenced object only.
        raw = text.strip()
        if raw.startswith('```') and raw.endswith('```'):
            raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
        try:
            schema = Quiz if mode == 'quiz' else Cards
            parsed = schema.model_validate_json(raw)
            return parsed.model_dump_json()
        except (ValidationError, ValueError):
            raise StudyAIError('Модель вернула некорректную структуру. Повторите действие; результат не сохранён.', 'schema') from None
    return text.strip()


class StudyAI:
    def __init__(self, client: GigaChatClient):
        self.client = client

    async def generate(self, path: Path, mode: str) -> GeneratedMaterial:
        if mode not in PROMPTS:
            raise StudyAIError('Неизвестное действие.', 'mode')
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise StudyAIError(SUPPORTED_FORMATS_MESSAGE, 'document')
        preparation = asyncio.create_task(asyncio.to_thread(prepare_content, path))
        try:
            prepared = await asyncio.shield(preparation)
        except asyncio.CancelledError:
            # Do not release the job's file while a local reader still uses it.
            try:
                await preparation
            except Exception:
                pass
            raise
        except (DocumentError, OSError, UnicodeError) as error:
            message = str(error) if isinstance(error, DocumentError) else 'Не удалось прочитать файл. Загрузите его заново.'
            raise StudyAIError(message, 'document') from None
        user = {'role': 'user', 'content': PROMPTS[mode]}
        if prepared.text:
            user['content'] += '\n\nМатериал лекции:\n' + prepared.text
        attachment = None
        try:
            if prepared.attachment:
                attachment = await self.client.upload(prepared.attachment, prepared.mime)
                user['attachments'] = [attachment]
            data = await self.client.complete({
                'model': self.client.settings.model,
                'messages': [{'role': 'system', 'content': SYSTEM}, user],
                'function_call': 'auto' if attachment else 'none',
                'max_tokens': 6000 if mode in {'outline', 'quiz', 'cards'} else 2400,
            })
            try:
                choice = data['choices'][0]
                text = choice['message']['content']
                if choice.get('finish_reason') == 'length':
                    if mode in {'quiz', 'cards'}:
                        raise StudyAIError('Структурированный ответ обрезан моделью. Попробуйте меньший документ.', 'response')
                    text += '\n\nОтвет достиг лимита модели и может быть неполным.'
                content = validate_result(text, mode)
                usage = {k: v for k, v in (data.get('usage') or {}).items()
                         if k in {'prompt_tokens', 'completion_tokens', 'total_tokens', 'precached_prompt_tokens'}
                         and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
            except (KeyError, IndexError, TypeError, AttributeError):
                raise StudyAIError('GigaChat вернул некорректный ответ.', 'response') from None
            return GeneratedMaterial(content, self.client.settings.model, usage or None)
        finally:
            if attachment:
                self.client.clean_later(attachment)


async def generate_study_material(path, mode, client: GigaChatClient | None = None) -> str:
    """Convenience entry point; the application injects its long-lived client/queue."""
    if client is not None:
        return (await StudyAI(client).generate(Path(path), mode)).content
    owned = GigaChatClient(Settings.from_env())
    await owned.open()
    try:
        async with asyncio.timeout(owned.settings.execution_timeout):
            return (await StudyAI(owned).generate(Path(path), mode)).content
    except asyncio.TimeoutError:
        raise StudyAIError('GigaChat не ответил вовремя. Повторите позже.', 'timeout') from None
    finally:
        await owned.close()

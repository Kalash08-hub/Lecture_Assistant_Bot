"""Validate uploaded formats and prepare text/attachments for the AI client."""
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.services.archive_text import DocumentError, read_xml, MAX_TEXT_SIZE
from app.services.document_text import extract_docx_text, extract_odt_text
from app.services.presentation_text import extract_odp_text

ATTACHMENT_MIME_TYPES = {
    '.pdf': 'application/pdf', '.ppt': 'application/vnd.ms-powerpoint',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
}
TEXT_READERS = {'.odt': extract_odt_text, '.docx': extract_docx_text, '.odp': extract_odp_text}
SUPPORTED_EXTENSIONS = {'.txt', *TEXT_READERS, *ATTACHMENT_MIME_TYPES}
SUPPORTED_FORMATS_MESSAGE = 'Для генерации доступны TXT, PDF, DOCX, ODT, PPT, PPTX и ODP.'


def txt_text(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > MAX_TEXT_SIZE or b'\x00' in raw:
        raise DocumentError('TXT слишком большой или содержит двоичные данные.')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        try:
            text = raw.decode('cp1251')
        except UnicodeDecodeError:
            raise DocumentError('Не удалось прочитать TXT. Сохраните его в UTF-8.') from None
    if not text.strip():
        raise DocumentError('Лекция пуста. Загрузите файл с текстом.')
    return text


@dataclass(frozen=True)
class PreparedContent:
    text: str | None = None
    attachment: Path | None = None
    mime: str | None = None


def prepare_content(path: Path) -> PreparedContent:
    suffix = path.suffix.lower()
    if suffix == '.txt':
        return PreparedContent(text=txt_text(path))
    if suffix in TEXT_READERS:
        return PreparedContent(text=TEXT_READERS[suffix](path))
    if suffix in ATTACHMENT_MIME_TYPES:
        return PreparedContent(attachment=path, mime=ATTACHMENT_MIME_TYPES[suffix])
    raise DocumentError(SUPPORTED_FORMATS_MESSAGE)


def validate_file(path: Path) -> str:
    suffix = path.suffix.lower()
    with path.open('rb') as stream:
        header = stream.read(1024)
    if not header:
        raise DocumentError('Файл пуст. Загрузите документ с содержимым.')
    if suffix in TEXT_READERS or suffix == '.txt':
        prepare_content(path)
    elif suffix == '.pdf':
        if b'%PDF-' not in header:
            raise DocumentError('Файл не похож на PDF. Пересохраните документ.')
        # Practical encryption-marker check, not a full PDF renderer.
        if b'/Encrypt' in path.read_bytes():
            raise DocumentError('PDF зашифрован. Снимите пароль и загрузите заново.')
    elif suffix == '.pptx':
        root = read_xml(path, 'ppt/presentation.xml')
        ns = '{http://schemas.openxmlformats.org/presentationml/2006/main}'
        slides = root.find(ns + 'sldIdLst')
        if root.tag != ns + 'presentation' or slides is None or not len(slides):
            raise DocumentError('В PPTX нет слайдов или структура файла повреждена.')
    elif suffix == '.ppt':
        if not header.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
            raise DocumentError('Неверная сигнатура PPT. Пересохраните презентацию в PPTX или PDF.')
    else:
        raise DocumentError(SUPPORTED_FORMATS_MESSAGE)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            digest.update(block)
    return digest.hexdigest()

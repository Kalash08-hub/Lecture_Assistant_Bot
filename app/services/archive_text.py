"""Bounded ZIP/XML reading shared by Office and OpenDocument formats."""
from pathlib import Path
from zipfile import BadZipFile, ZipFile
from zlib import error as ZlibError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

MAX_XML_SIZE = 16 * 1024 * 1024
MAX_UNPACKED_SIZE = 32 * 1024 * 1024
MAX_TEXT_SIZE = 2 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 5000


class DocumentError(ValueError):
    pass


def check_archive(archive: ZipFile) -> None:
    entries = archive.infolist()
    if len(entries) > MAX_ARCHIVE_ENTRIES or sum(i.file_size for i in entries) > MAX_UNPACKED_SIZE:
        raise DocumentError('Распакованный документ слишком большой. Разделите его на несколько файлов.')
    if any(i.flag_bits & 1 for i in entries):
        raise DocumentError('Документ зашифрован. Снимите пароль и загрузите файл заново.')


def read_xml(path: Path, entry: str, limit: int = MAX_XML_SIZE):
    try:
        with ZipFile(path) as archive:
            check_archive(archive)
            if archive.getinfo(entry).file_size > limit:
                raise DocumentError('Содержимое документа слишком большое. Разделите его на несколько файлов.')
            with archive.open(entry) as stream:
                content = stream.read(limit + 1)
            if len(content) > limit:
                raise DocumentError('Содержимое документа слишком большое. Разделите его на несколько файлов.')
        root = ElementTree.fromstring(content)
        nodes = 0
        stack = [(root, 1)]
        while stack:
            node, depth = stack.pop()
            nodes += 1
            if nodes > 200_000 or depth > 64:
                raise DocumentError('Структура документа слишком сложная. Разделите его на несколько файлов.')
            stack.extend((child, depth + 1) for child in node)
        return root
    except DocumentError:
        raise
    except (BadZipFile, KeyError, RuntimeError, NotImplementedError, ZlibError, EOFError,
            ElementTree.ParseError, DefusedXmlException, RecursionError):
        raise DocumentError('Документ повреждён, зашифрован или имеет неверный формат. Пересохраните его и загрузите заново.') from None


def text_of(node, prefix: str) -> str:
    parts: list[str] = []
    size = 0

    def append(text: str) -> None:
        nonlocal size
        size += len(text)
        if size > MAX_TEXT_SIZE:
            raise DocumentError('Извлечённый текст слишком большой. Разделите документ.')
        parts.append(text)

    # An explicit stack avoids recursion limits on maliciously nested documents.
    stack = [(node, False)]
    while stack:
        element, tail = stack.pop()
        if tail:
            append(element.tail or '')
            continue
        if element.tag == prefix + 's':
            try:
                count = int(element.get(prefix + 'c', '1'))
            except ValueError:
                raise DocumentError('Некорректные пробелы в документе.') from None
            if not 1 <= count <= MAX_TEXT_SIZE - size:
                raise DocumentError('Извлечённый текст слишком большой или повреждён.')
            append(' ' * count)
        elif element.tag in {prefix + 'tab'}:
            append('\t')
        elif element.tag in {prefix + 'line-break', prefix + 'br', prefix + 'cr'}:
            append('\n')
        else:
            append(element.text or '')
            for child in reversed(list(element)):
                stack.append((child, True))
                stack.append((child, False))
    return ''.join(parts).strip()


def bounded_join(paragraphs) -> str:
    parts = []
    size = 0
    for paragraph in paragraphs:
        size += len(paragraph) + 1
        if size > MAX_TEXT_SIZE:
            raise DocumentError('Извлечённый текст слишком большой. Разделите документ.')
        parts.append(paragraph)
    result = '\n'.join(parts).strip()
    if not result:
        raise DocumentError('В документе нет доступного текста. Загрузите документ с текстом или PDF.')
    return result

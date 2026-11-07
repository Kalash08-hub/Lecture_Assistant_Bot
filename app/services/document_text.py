"""Read text from ODT without an office installation or external API upload."""
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile
from zlib import error as ZlibError

OFFICE = '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}'
TEXT = '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}'
MAX_XML_SIZE = 16 * 1024 * 1024
MAX_TEXT_SIZE = 4 * 1024 * 1024


class DocumentTextError(ValueError):
    pass


def _paragraph_text(node):
    parts = []
    size = 0

    def append(value):
        nonlocal size
        size += len(value)
        if size > MAX_TEXT_SIZE:
            raise DocumentTextError('ODT слишком большой. Разделите документ на несколько файлов.')
        parts.append(value)

    def visit(element):
        if element.tag == TEXT + 's':
            count = int(element.get(TEXT + 'c', '1'))
            if not 1 <= count <= MAX_TEXT_SIZE:
                raise ValueError('Invalid space count')
            append(' ' * count)
        elif element.tag == TEXT + 'tab':
            append('\t')
        elif element.tag == TEXT + 'line-break':
            append('\n')
        else:
            append(element.text or '')
            for child in element:
                visit(child)
                append(child.tail or '')

    visit(node)
    return ''.join(parts).strip()


def extract_odt_text(path):
    try:
        with ZipFile(path) as archive:
            info = archive.getinfo('content.xml')
            if info.file_size > MAX_XML_SIZE:
                raise DocumentTextError('ODT слишком большой. Разделите документ на несколько файлов.')
            with archive.open(info) as stream:
                content = stream.read(MAX_XML_SIZE + 1)
            if len(content) > MAX_XML_SIZE:
                raise DocumentTextError('ODT слишком большой. Разделите документ на несколько файлов.')
        root = ElementTree.fromstring(content)
        body = root.find(OFFICE + 'body/' + OFFICE + 'text')
        if body is None:
            raise ValueError('Not an ODT text document')
        paragraphs = []
        size = 0
        for node in body.iter():
            if node.tag in (TEXT + 'h', TEXT + 'p'):
                paragraph = _paragraph_text(node)
                size += len(paragraph) + 1
                if size > MAX_TEXT_SIZE:
                    raise DocumentTextError('ODT слишком большой. Разделите документ на несколько файлов.')
                if paragraph:
                    paragraphs.append(paragraph)
    except DocumentTextError:
        raise
    except (BadZipFile, KeyError, ElementTree.ParseError, RuntimeError,
            NotImplementedError, ValueError, ZlibError, EOFError, RecursionError):
        raise DocumentTextError('Не удалось прочитать ODT. Пересохраните документ в ODT, TXT или PDF.') from None
    if not paragraphs:
        raise DocumentTextError('В ODT нет доступного текста. Загрузите документ с текстом или PDF.')
    return '\n'.join(paragraphs)

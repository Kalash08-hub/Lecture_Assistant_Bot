"""Local text extraction; images and embedded equation objects are not recognized."""
from pathlib import Path

from app.services.archive_text import DocumentError, MAX_XML_SIZE, bounded_join, read_xml, text_of

OFFICE = '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}'
TEXT = '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}'
WORD = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
DocumentTextError = DocumentError


def extract_odt_text(path: Path) -> str:
    root = read_xml(path, 'content.xml', MAX_XML_SIZE)
    body = root.find(OFFICE + 'body/' + OFFICE + 'text')
    if body is None:
        raise DocumentError('Это не текстовый документ ODT. Пересохраните файл.')
    return bounded_join(text_of(node, TEXT) for node in body.iter() if node.tag in {TEXT + 'p', TEXT + 'h'})


def extract_docx_text(path: Path) -> str:
    root = read_xml(path, 'word/document.xml')
    body = root.find(WORD + 'body')
    if body is None:
        raise DocumentError('В DOCX нет текста документа.')
    # XML traversal preserves paragraph/table-cell order, including nested tables.
    return bounded_join(text_of(node, WORD) for node in body.iter(WORD + 'p'))

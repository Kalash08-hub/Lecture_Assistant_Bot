"""Extract OpenDocument slide text, preserving whitespace and slide boundaries."""
from pathlib import Path

from app.services.archive_text import DocumentError, MAX_XML_SIZE, bounded_join, read_xml, text_of
from app.services.document_text import OFFICE, TEXT

DRAW = '{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}'
PresentationTextError = DocumentError


def extract_odp_text(path: Path) -> str:
    root = read_xml(path, 'content.xml', MAX_XML_SIZE)
    presentation = root.find(OFFICE + 'body/' + OFFICE + 'presentation')
    if presentation is None:
        raise DocumentError('Это не презентация ODP. Пересохраните файл.')
    slides = []
    output_size = 0
    for index, page in enumerate(presentation.findall(DRAW + 'page'), 1):
        paragraphs = (text_of(node, TEXT) for node in page.iter() if node.tag in {TEXT + 'p', TEXT + 'h'})
        parts = []
        total = 0
        for paragraph in paragraphs:
            total += len(paragraph) + 1
            if total > 2 * 1024 * 1024:
                raise DocumentError('Извлечённый текст слишком большой. Разделите презентацию.')
            parts.append(paragraph)
        if any(parts):
            text = bounded_join(parts)
            slide = f'Слайд {index}\n{text}'
            output_size += len(slide)
            slides.append(slide)
            if output_size > 2 * 1024 * 1024:
                raise DocumentError('Извлечённый текст слишком большой. Разделите презентацию.')
    return bounded_join(slides)

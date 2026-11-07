"""Extract slide text from OpenDocument presentations without office software."""
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

DRAW = '{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}'
TEXT = '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}'
MAX_XML_SIZE = 16 * 1024 * 1024


class PresentationTextError(ValueError):
    pass


def extract_odp_text(path):
    try:
        with ZipFile(path) as archive:
            info = archive.getinfo('content.xml')
            if info.file_size > MAX_XML_SIZE:
                raise PresentationTextError('ODP слишком большой. Разделите презентацию на несколько файлов.')
            with archive.open(info) as stream:
                content = stream.read(MAX_XML_SIZE + 1)
            if len(content) > MAX_XML_SIZE:
                raise PresentationTextError('ODP слишком большой. Разделите презентацию на несколько файлов.')
        root = ElementTree.fromstring(content)
    except (BadZipFile, KeyError, ElementTree.ParseError, RuntimeError, NotImplementedError):
        raise PresentationTextError('Не удалось прочитать ODP. Пересохраните презентацию в ODP, PPTX или PDF.') from None
    slides = []
    for index, page in enumerate(root.iter(DRAW + 'page'), 1):
        paragraphs = []
        for node in page.iter():
            if node.tag in (TEXT + 'p', TEXT + 'h'):
                text = ''.join(node.itertext()).strip()
                if text:
                    paragraphs.append(text)
        if paragraphs:
            slides.append(f'Слайд {index}\n' + '\n'.join(paragraphs))
    if not slides:
        raise PresentationTextError('В ODP нет доступного текста. Сохраните презентацию в PDF и загрузите заново.')
    return '\n\n'.join(slides)

"""Bounded large lecture downloads and media-free PowerPoint extraction."""
from __future__ import annotations

import os
import posixpath
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import requests

SMALL_FILE_BYTES = 25 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_TOTAL_XML_BYTES = 64 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000


class LectureDownloadError(ValueError):
    def __init__(self, status=None):
        super().__init__('Lecture download failed')
        self.status = status


def lecture_file_limit() -> int:
    value = int(os.getenv('LECTURE_MAX_FILE_MB', '768'))
    if not 1 <= value <= 1024:
        raise ValueError('LECTURE_MAX_FILE_MB must be between 1 and 1024')
    return value * 1024 * 1024


def stream_download(url: str, destination: Path, limit: int) -> None:
    """Use Canvas's signed file URL; never forward the API bearer token."""
    deadline = time.monotonic() + 300
    for attempt in range(3):
        try:
            current = url
            for redirect in range(6):
                if time.monotonic() > deadline:
                    raise ValueError('Lecture download exceeds time limit')
                parsed = urlparse(current)
                if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError('Invalid lecture download URL')
                with requests.get(current, stream=True, timeout=(10, 30), allow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        current = urljoin(current, response.headers.get('Location', ''))
                        continue
                    response.raise_for_status()
                    if int(response.headers.get('Content-Length', '0')) > limit:
                        raise ValueError('Lecture download exceeds byte limit')
                    size = 0
                    with destination.open('wb') as output:
                        for block in response.iter_content(chunk_size=1024 * 1024):
                            size += len(block)
                            if size > limit or time.monotonic() > deadline:
                                raise ValueError('Lecture download exceeds byte or time limit')
                            output.write(block)
                    return
            raise ValueError('Too many lecture download redirects')
        except requests.RequestException as error:
            destination.unlink(missing_ok=True)
            status = error.response.status_code if error.response is not None else None
            if attempt == 2 or (status is not None and status < 500 and status != 429):
                raise LectureDownloadError(status) from None
            time.sleep(2 ** attempt)
        except Exception:
            destination.unlink(missing_ok=True)
            raise


def powerpoint_slide_text(path: Path) -> list[tuple[int, str]]:
    """Read only ordered slide/notes XML; never inflate embedded media."""
    ns = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
          'p': 'http://schemas.openxmlformats.org/presentationml/2006/main'}
    rid = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
    total = 0
    with zipfile.ZipFile(path) as archive:
        def xml(name):
            nonlocal total
            info = archive.getinfo(name)
            total += info.file_size
            if info.file_size > MAX_XML_BYTES or total > MAX_TOTAL_XML_BYTES:
                raise ValueError('PowerPoint XML exceeds extraction limit')
            return ET.fromstring(archive.read(info))

        def relationships(part):
            name = posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part) + '.rels')
            if name not in archive.namelist():
                return {}
            result = {}
            for item in xml(name):
                if item.get('TargetMode') == 'External':
                    continue
                target = item.get('Target', '')
                target = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
                if not target.startswith('ppt/'):
                    raise ValueError('Invalid PowerPoint relationship')
                result[item.get('Id')] = (target, item.get('Type', ''))
            return result

        presentation = xml('ppt/presentation.xml')
        rels = relationships('ppt/presentation.xml')
        result, chars = [], 0
        for number, slide in enumerate(presentation.findall('p:sldIdLst/p:sldId', ns), 1):
            target, _ = rels[slide.get(rid)]
            root = xml(target)
            parts = []
            for paragraph in root.findall('.//a:p', ns):
                text = ''.join(t.text or '' for t in paragraph.findall('.//a:t', ns)).strip()
                if text:
                    parts.append(text)
            for note, kind in relationships(target).values():
                if not kind.endswith('/notesSlide'):
                    continue
                notes = xml(note)
                for shape in notes.findall('.//p:sp', ns):
                    placeholder = shape.find('.//p:ph', ns)
                    if placeholder is not None and placeholder.get('type') in ('sldNum', 'hdr', 'ftr', 'dt', 'sldImg'):
                        continue
                    text = ' '.join(t.text or '' for t in shape.findall('.//a:t', ns)).strip()
                    if text:
                        parts.append('Speaker notes: ' + text)
            text = '\n'.join(parts)
            chars += len(text)
            if chars > MAX_TEXT_CHARS:
                raise ValueError('PowerPoint text exceeds extraction limit')
            if text:
                result.append((number, text))
        return result

"""Read protected lecture slides with explicitly granted Google Slides access."""
from __future__ import annotations

import json
import os
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

SLIDES_SCOPE = 'https://www.googleapis.com/auth/presentations.readonly'


class SlidesAccessError(ValueError):
    """Fixed, safe diagnostic for missing Google access."""


def authenticated_slide_text(document_id: str) -> str:
    path = Path(os.getenv('GOOGLE_TOKEN_FILE', 'token.json')).expanduser()
    if not path.is_file():
        raise SlidesAccessError('GOOGLE_SLIDES_AUTH_REQUIRED')
    data = json.loads(path.read_text())
    if SLIDES_SCOPE not in data.get('scopes', []):
        raise SlidesAccessError('GOOGLE_SLIDES_AUTH_REQUIRED')
    credentials = Credentials.from_authorized_user_info(data)
    try:
        with AuthorizedSession(credentials) as session:
            with session.get(
                f'https://slides.googleapis.com/v1/presentations/{document_id}',
                params={'fields': 'slides(pageElements,slideProperties(notesPage(pageElements)))'},
                timeout=(10, 30), stream=True,
            ) as response:
                if response.status_code in (401, 403, 404):
                    raise SlidesAccessError('GOOGLE_SLIDES_ACCESS_DENIED')
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > 16 * 1024 * 1024:
                        raise ValueError('Google Slides response exceeds extraction limit')
                value = json.loads(body)
    except SlidesAccessError:
        raise
    except Exception:
        raise ValueError('Google Slides API read failed') from None

    def texts(node):
        if isinstance(node, dict):
            if 'textRun' in node:
                yield node['textRun'].get('content', '')
            else:
                for item in node.values():
                    yield from texts(item)
        elif isinstance(node, list):
            for item in node:
                yield from texts(item)

    parts = [(i, ''.join(texts(slide)).strip()) for i, slide in enumerate(value.get('slides', []), 1)]
    text = '\n\n'.join(f'[Slide {i}]\n{content}' for i, content in parts if content)
    if not text.strip() or len(text) > 2_000_000:
        raise ValueError('Google Slides text is empty or exceeds extraction limit')
    return text

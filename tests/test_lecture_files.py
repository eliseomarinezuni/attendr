from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
import zipfile

import pytest
import requests
from pptx import Presentation

from academic_assistant.lecture_files import stream_download, powerpoint_slide_text
from academic_assistant.ai_assistant import extract_powerpoint_text_chunks
from academic_assistant.canvas_client import CanvasClient
from academic_assistant.state_store import StateStore


def deck(path):
    p = Presentation()
    slide = p.slides.add_slide(p.slide_layouts[1])
    slide.shapes.title.text = 'Lecture 1'
    slide.placeholders[1].text = 'Matrices and transformations'
    slide.notes_slide.notes_text_frame.text = 'Remember multiplication order'
    p.save(path)


def test_extraction_does_not_read_embedded_media(tmp_path, monkeypatch):
    path = tmp_path / 'deck.pptx'
    deck(path)
    with zipfile.ZipFile(path, 'a') as archive:
        archive.writestr('ppt/media/huge.mp4', b'ignored media')
    original = zipfile.ZipFile.read
    def read(self, name, *args, **kwargs):
        assert not (name.filename if hasattr(name, 'filename') else name).startswith('ppt/media/')
        return original(self, name, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, 'read', read)
    text = '\n'.join(c.text for c in extract_powerpoint_text_chunks(path))
    assert 'Matrices and transformations' in text
    assert 'Remember multiplication order' in text


def test_xml_limit_rejects_before_read(tmp_path, monkeypatch):
    path = tmp_path / 'deck.pptx'
    deck(path)
    monkeypatch.setattr('academic_assistant.lecture_files.MAX_XML_BYTES', 10)
    with pytest.raises(ValueError, match='extraction limit'):
        powerpoint_slide_text(path)


def response(blocks, status=200, headers=None):
    r = Mock()
    r.__enter__ = Mock(return_value=r)
    r.__exit__ = Mock(return_value=False)
    r.status_code, r.headers = status, headers or {}
    r.iter_content.return_value = iter(blocks)
    return r


def test_stream_counts_actual_bytes_and_removes_partial(tmp_path, monkeypatch):
    r = response([b'123', b'456'])
    get = Mock(return_value=r)
    monkeypatch.setattr(requests, 'get', get)
    path = tmp_path / 'download'
    with pytest.raises(ValueError, match='byte or time'):
        stream_download('https://canvas.test/file', path, 5)
    assert not path.exists()
    assert get.call_args.kwargs['stream'] is True
    assert 'headers' not in get.call_args.kwargs


def test_stream_rejects_insecure_redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, 'get', Mock(return_value=response([], 302, {'Location': 'http://unsafe.test'})))
    with pytest.raises(ValueError, match='Invalid lecture'):
        stream_download('https://canvas.test/file', tmp_path / 'download', 100)


def test_stream_retries_transport_failure(tmp_path, monkeypatch):
    r = response([b'content'])
    get = Mock(side_effect=[requests.Timeout('secret'), r])
    monkeypatch.setattr(requests, 'get', get)
    monkeypatch.setattr('academic_assistant.lecture_files.time.sleep', lambda _: None)
    path = tmp_path / 'download'
    stream_download('https://canvas.test/file', path, 100)
    assert path.read_bytes() == b'content'
    assert get.call_count == 2


def test_large_deck_cache_survives_new_runner_and_invalidates_revision(tmp_path, monkeypatch):
    source = tmp_path / 'original.pptx'
    deck(source)
    download = Mock(side_effect=lambda url, dest, limit: dest.write_bytes(source.read_bytes()))
    monkeypatch.setattr('academic_assistant.canvas_client.stream_download', download)
    file = NS(size=518508549, updated_at='2026-09-09T00:00:00Z', url='https://canvas.test/signed')
    course = NS(id=1, name='Graphics')
    def client():
        c = object.__new__(CanvasClient)
        c.base_url = 'https://canvas.test'
        c.state_store = StateStore(tmp_path / 'state.db')
        return c
    args = (file, course, '8', 'Lecture 1', tmp_path / 'materials', 768 * 1024**2, 'Week 1', 1, 1, False)
    material = client()._large_lecture_text(*args)
    assert 'Matrices' in material.local_path.read_text()
    material.local_path.unlink()
    assert client()._large_lecture_text(*args).local_path.exists()
    assert download.call_count == 1
    file.updated_at = '2026-09-10T00:00:00Z'
    client()._large_lecture_text(*args)
    assert download.call_count == 2
    assert not list((tmp_path / 'materials').glob('lecture-*'))


def test_quiz_reads_cached_text_literally(tmp_path):
    from academic_assistant.lecture_quiz import LectureQuizRunner
    path = tmp_path / 'source.txt'
    path.write_text('Compare x < y and x > z')
    assert LectureQuizRunner._material_text(NS(local_path=path)) == path.read_text()


def test_google_slides_cache_and_login_page_rejection(tmp_path, monkeypatch):
    c = object.__new__(CanvasClient)
    c.base_url = 'https://canvas.test'
    c.state_store = StateStore(tmp_path / 'state.db')
    download = Mock(side_effect=lambda url, dest, limit: dest.write_text('Protocols and HTTP'))
    monkeypatch.setattr('academic_assistant.canvas_client.stream_download', download)
    args = (NS(id=1, name='Web'), 'https://docs.google.com/presentation/d/abc/edit',
            '01a - protocols', tmp_path, 'Basics', 2, 1)
    assert 'HTTP' in c._download_external_slides(*args).local_path.read_text()
    c._download_external_slides(*args)
    assert download.call_count == 1
    c.state_store = None
    download.side_effect = lambda url, dest, limit: dest.write_text('<!DOCTYPE html><html>Sign in</html>')
    with pytest.raises(ValueError, match='unavailable'):
        c._download_external_slides(*args)
    assert c._download_external_slides(NS(id=1), 'https://unrelated.test', '', tmp_path, '', 1, 1) is None


def test_numbered_week_labels_match_correct_session(tmp_path):
    from datetime import datetime
    from academic_assistant.course_schedule import CourseSchedule
    from academic_assistant.lecture_quiz import LectureQuizRunner
    root = Path(__file__).resolve().parents[1]
    schedule = CourseSchedule.load(root / 'data/course_schedule.json')
    session = next(s for s in schedule.sessions if s.course_key == 'web-development' and s.weekday == 1 and s.activity == 'lecture')
    runner = LectureQuizRunner(None, None, None, schedule, materials_directory=tmp_path, state_path=tmp_path / 'state.db')
    common = dict(course_name='Web Development', module_name='Basics', module_position=2, updated_at=None)
    first = NS(title='01a - protocols', item_position=1, **common)
    second = NS(title='01b - HTML', item_position=2, **common)
    ended = datetime(2026, 9, 8, 14, tzinfo=schedule.timezone)
    assert runner._select_material(session, ended, (second, first)) is first
    assert runner._select_material(session, ended, (second,)) is None
    due = schedule.ended_lecture_sessions(datetime(2026, 9, 17, 22, tzinfo=schedule.timezone), retry_hours=336)
    assert any(day.date() == ended.date() and s == session for s, day in due)


def test_private_slides_requires_explicit_scope(tmp_path, monkeypatch):
    import json
    from academic_assistant.google_slides import authenticated_slide_text, SlidesAccessError
    path = tmp_path / 'token.json'
    path.write_text(json.dumps({'scopes': ['https://www.googleapis.com/auth/calendar']}))
    monkeypatch.setenv('GOOGLE_TOKEN_FILE', str(path))
    with pytest.raises(SlidesAccessError, match='AUTH_REQUIRED'):
        authenticated_slide_text('test')


def test_private_slides_falls_back_to_authorized_reader(tmp_path, monkeypatch):
    from academic_assistant.lecture_files import LectureDownloadError
    c = object.__new__(CanvasClient)
    c.state_store = None
    monkeypatch.setattr('academic_assistant.canvas_client.stream_download', Mock(side_effect=LectureDownloadError(401)))
    reader = Mock(return_value='Authenticated lecture text')
    monkeypatch.setattr('academic_assistant.canvas_client.authenticated_slide_text', reader)
    result = c._download_external_slides(NS(id=1, name='Web'),
        'https://docs.google.com/presentation/d/abc/edit', '01a', tmp_path, '', 1, 1)
    assert result.local_path.read_text() == 'Authenticated lecture text'
    reader.assert_called_once_with('abc')


def test_teaching_week_skips_reading_week_and_starts_monday():
    from datetime import date
    from academic_assistant.course_schedule import CourseSchedule
    root = Path(__file__).resolve().parents[1]
    schedule = CourseSchedule.load(root / 'data/course_schedule.json')
    assert schedule.teaching_week(date(2026, 9, 14)) == 2
    assert schedule.teaching_week(date(2026, 10, 19)) == 6
    assert schedule.teaching_week(date(2026, 11, 30)) == 12


def test_one_bad_lecture_does_not_block_other_files(tmp_path):
    c = object.__new__(CanvasClient)
    course = NS(id=1, name='Course', course_code='')
    files = [NS(id=1, display_name='Lecture1.pdf'), NS(id=2, display_name='Lecture2.pdf')]
    resource = NS(get_modules=lambda **kw: [], get_files=lambda **kw: files)
    c._get_active_course_contexts = lambda: [NS(summary=course, resource=resource)]
    good = NS(uid='canvas:lecture-file:1:2')
    def download(*args):
        if args[2] == '1':
            raise ValueError('private body')
        return good
    c._download_lecture_file = download
    report = c.download_lecture_materials(tmp_path)
    assert report.materials == (good,)
    assert len(report.warnings) == 1
    assert 'private' not in report.warnings[0]

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
import json

import pytest
from canvasapi.exceptions import Unauthorized

from academic_assistant.knowledge_sync import KnowledgeSync, KnowledgeSyncError, chunks, match_course, record
from scripts.setup_ask import setup, COMMAND

ROOT = Path(__file__).resolve().parents[1]
SCHEDULE = json.loads((ROOT / 'data/course_schedule.json').read_text())
COURSE = next(c for c in SCHEDULE['courses'] if c['key'] == 'web-development')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Live network forbidden')
    monkeypatch.setattr('requests.sessions.Session.request', blocked)


class Store:
    def __init__(self): self.cache = {}
    def cache_get(self, key): return self.cache.get(key)
    def cache_set(self, key, value): self.cache[key] = value


class Session:
    def __init__(self, statuses=(200,)): self.calls, self.statuses = [], iter(statuses)
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return NS(status_code=next(self.statuses, 200))


def build(tmp_path, session=None):
    page = NS(url='intro', title='Introduction', body='HTTP is stateless.', updated_at=None)
    assignment = NS(id=11, name='Midterm', description='Course assessment', due_at='2026-10-20T18:00:00Z')
    topic = NS(id=21, title='Welcome', message='Read the syllabus', posted_at='2026-09-01T12:00:00Z')
    api = NS(get_modules=lambda: [], get_pages=lambda: [page], get_page=lambda key: page,
             get_files=lambda: [], get_assignments=lambda **kw: [assignment],
             get_discussion_topics=lambda **kw: [topic])
    summary = NS(id=1, name='Web Development', course_code='CSCI 3230')
    context = NS(resource=api, summary=summary)
    canvas = NS(base_url='https://canvas.example', _get_active_course_contexts=lambda: [context],
                _attr=lambda value, key, default=None: getattr(value, key, default),
                _html_to_text=lambda text: text,
                _canvas=NS(get_course=lambda *a, **kw: NS(syllabus_body='Midterm October 20, 2026')))
    sync = KnowledgeSync(canvas, Store(), SCHEDULE, tmp_path, 'https://worker.test', 'secret', session or Session())
    return sync, context, page, assignment, topic


@pytest.mark.parametrize('course', SCHEDULE['courses'])
def test_all_course_aliases(course):
    for alias in [course['key'], course['name'], *course['match']]:
        assert match_course(alias.upper(), '', SCHEDULE['courses'])['key'] == course['key']
    assert match_course('Unconfigured course', '', SCHEDULE['courses']) is None
    with pytest.raises(KnowledgeSyncError):
        match_course('web dev algorithms', '', SCHEDULE['courses'])


def test_complete_snapshots_include_all_sources_and_metadata(tmp_path):
    sync, context, *_ = build(tmp_path)
    assert sync.sync() == 1
    payload = sync.session.calls[0][1]['json']
    assert payload['complete'] is True
    assert payload['course']['key'] == 'web-development'
    records = {r['id'].rsplit(':', 1)[0]: r for r in payload['records']}
    assert set(records) == {'syllabus', 'page:intro', 'assignment:11', 'announcement:21', 'verified-schedule'}
    assert records['assignment:11']['deadline'] == '2026-10-20T18:00:00Z'
    assert 'America/Toronto' in records['verified-schedule']['chunks'][0]
    assert all(len(r['hash']) == 64 for r in records.values())


def test_updated_deleted_unpublished_sources_and_duplicate_sync(tmp_path):
    sync, context, page, *_ = build(tmp_path)
    before = {r['id']: r for r in sync.collect(context, COURSE)}
    assert before == {r['id']: r for r in sync.collect(context, COURSE)}
    page.body = 'Updated HTTP course material'
    after = {r['id']: r for r in sync.collect(context, COURSE)}
    assert before['page:intro']['hash'] != after['page:intro']['hash']
    page.published = False
    assert 'page:intro' not in {r['id'] for r in sync.collect(context, COURSE)}
    context.resource.get_pages = lambda: []
    assert 'page:intro' not in {r['id'] for r in sync.collect(context, COURSE)}


def test_partial_enumeration_and_download_failures_do_not_publish(tmp_path):
    sync, context, *_ = build(tmp_path)
    def broken():
        yield NS(url='intro')
        raise RuntimeError('private upstream detail')
    context.resource.get_pages = broken
    with pytest.raises(KnowledgeSyncError, match='previous complete snapshots preserved') as error:
        sync.sync()
    assert 'private' not in str(error.value)
    assert not sync.session.calls
    context.resource.get_pages = lambda: []
    context.resource.get_files = lambda: [NS(id=8, filename='lecture.pdf')]
    sync.canvas._download_lecture_file = lambda *args: None
    with pytest.raises(KnowledgeSyncError): sync.sync()
    assert not sync.session.calls


def test_retry_payloads_are_identical(tmp_path):
    sync, *_ = build(tmp_path, Session([503, 503, 200]))
    assert sync.sync() == 1
    assert len(sync.session.calls) == 4
    assert sync.session.calls[0][1]['json'] == sync.session.calls[2][1]['json']


def test_cached_extraction_is_not_reprocessed(tmp_path, monkeypatch):
    sync, *_ = build(tmp_path)
    material = NS(uid='file:1', content_sha256='hash', content_type='application/pdf', local_path=tmp_path / 'x.pdf')
    calls = []
    monkeypatch.setattr('academic_assistant.knowledge_sync.extract_pdf_text_chunks', lambda path: calls.append(path) or [NS(text='Extracted PDF text')])
    assert sync._text(material) == sync._text(material) == 'Extracted PDF text'
    assert len(calls) == 1
    material.content_sha256 = 'updated'
    sync._text(material)
    assert len(calls) == 2


def test_module_metadata_and_hidden_module_sources(tmp_path):
    sync, context, *_ = build(tmp_path)
    module = NS(name='Week 1', position=1, get_module_items=lambda: [NS(type='Page', page_url='intro', position=2)])
    context.resource.get_modules = lambda: [module]
    records = {r['id']: r for r in sync.collect(context, COURSE)}
    assert records['page:intro']['module'] == [{'name': 'Week 1', 'position': 1, 'item_position': 2}]
    module.published = False
    assert 'page:intro' not in {r['id'] for r in sync.collect(context, COURSE)}


def test_disabled_pages_and_files_tabs_still_use_visible_module_items(tmp_path):
    sync, context, page, *_ = build(tmp_path)
    file = NS(id=8, display_name='lecture.pdf', published=True)
    module = NS(
        name='Week 1',
        position=1,
        get_module_items=lambda: [
            NS(type='Page', page_url='intro', position=1, published=True),
            NS(type='File', content_id=8, position=2, published=True),
        ],
    )
    context.resource.get_modules = lambda: [module]
    context.resource.get_pages = lambda: (_ for _ in ()).throw(Unauthorized('pages disabled'))
    context.resource.get_files = lambda: (_ for _ in ()).throw(Unauthorized('files disabled'))
    context.resource.get_file = lambda key: file
    material = NS(
        uid='file:8', content_sha256='a' * 64, content_type='application/pdf',
        local_path=tmp_path / 'lecture.pdf', html_url='https://canvas.example/files/8',
        updated_at=None,
    )
    sync.canvas._download_lecture_file = lambda *args: material
    sync._text = lambda value: 'Accessible module lecture text'
    records = {item['id']: item for item in sync.collect(context, COURSE)}
    assert records['page:intro']['chunks'] == ['HTTP is stateless.']
    assert records['file:8']['chunks'] == ['Accessible module lecture text']


def test_chunk_bounds_and_url_secrets_removed():
    assert max(map(len, chunks('text ' * 10000))) <= 1400
    source = record('1', 'Title', 'page', 'Text', url='https://canvas.example/page?verifier=secret')
    assert source['url'] == 'https://canvas.example/page'
    assert record('1', 'Title', 'page', 'Text', url='https://user:secret@canvas.example')['url'] is None


class Discord:
    def __init__(self, existing=False): self.calls, self.existing = [], existing
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == 'GET' and url.endswith('/channels'):
            data = [{'id': '123', 'name': 'ask', 'type': 0}] if self.existing else []
        elif url.endswith('/users/@me'): data = {'id': '888'}
        else: data = {'id': '123'}
        return NS(ok=True, json=lambda: data)


@pytest.mark.parametrize('existing', [True, False])
def test_setup_reuses_channel_and_preserves_other_commands(existing):
    discord = Discord(existing)
    assert setup('secret', '555', '666', '777', discord) == '123'
    creates = [c for c in discord.calls if c[0] == 'POST' and c[1].endswith('/channels')]
    assert len(creates) == (0 if existing else 1)
    assert all(c[0] != 'PUT' for c in discord.calls)
    assert discord.calls[-1][2]['json'] == COMMAND
    assert len(COMMAND['options']) == 1
    if creates:
        assert creates[0][2]['json']['permission_overwrites'][0]['deny'] == '1024'


def test_scheduled_sync_keeps_encrypted_runner():
    workflow = (ROOT / '.github/workflows/schedule.yml').read_text()
    assert 'ATTENDR_ASK_SYNC: "true"' in workflow
    assert 'python scripts/cloud_run.py' in workflow
    assert 'ATTENDR_STATE_KEY:' in workflow


def test_large_course_batches_preserve_every_chunk_before_publish(tmp_path):
    sync, *_ = build(tmp_path)
    sources = [record(str(i), 'Material', 'page', 'long course text ' * 10000) for i in range(10)]
    sync.collect = lambda *args: sources
    assert sync.sync() == 1
    stages = [kwargs['json'] for url, kwargs in sync.session.calls if url.endswith('/stage')]
    assert len(stages) > 1
    assert all(len(json.dumps(payload).encode()) < 900_000 for payload in stages)
    assert sum(len(r['chunks']) for payload in stages for r in payload['records']) == sum(len(r['chunks']) for r in sources)
    assert sync.session.calls[-1][0].endswith('/publish')
    assert sync.session.calls[-1][1]['json']['count'] == sum(len(p['records']) for p in stages)


def test_failed_staging_never_publishes(tmp_path):
    sync, *_ = build(tmp_path, Session([503, 503, 503]))
    with pytest.raises(KnowledgeSyncError): sync.sync()
    assert all(url.endswith('/stage') for url, _ in sync.session.calls)


def test_powerpoint_extraction_is_cached(tmp_path, monkeypatch):
    sync, *_ = build(tmp_path)
    material = NS(uid='file:2', content_sha256='hash', content_type='presentation', local_path=tmp_path / 'x.pptx')
    calls = []
    monkeypatch.setattr('academic_assistant.knowledge_sync.extract_powerpoint_text_chunks', lambda path, **kwargs: calls.append(path) or [NS(text='PowerPoint slide text')])
    assert sync._text(material) == sync._text(material) == 'PowerPoint slide text'
    assert len(calls) == 1

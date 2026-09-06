import base64
import sqlite3
from pathlib import Path

import pytest

from academic_assistant import cloud_state
from academic_assistant.cloud_state import CloudStateClient, CloudStateError
from academic_assistant.state_store import StateStore


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.ok = status < 400

    def json(self):
        return self.payload


def test_encrypted_checkpoint_round_trip(monkeypatch, tmp_path):
    remote = {"revision": 0, "sha256": None, "size": 0, "chunks": []}

    def request(method, _url, **kwargs):
        if method == "GET":
            return Response(remote.copy())
        assert kwargs["json"]["revision"] == remote["revision"]
        remote.update(kwargs["json"])
        remote["revision"] += 1
        return Response({"revision": remote["revision"]})

    monkeypatch.setattr(cloud_state.requests, "request", request)
    key = "high-entropy-test-secret"
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE value(name TEXT)")
        db.execute("INSERT INTO value VALUES('private')")
    client = CloudStateClient("https://state.test", "secret", key, "a" * 32, 0)
    client.upload(source)
    assert b"private" not in base64.b64decode("".join(remote["chunks"]))
    target = tmp_path / "target.db"
    assert client.download(target)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT name FROM value").fetchone()[0] == "private"


def test_state_store_checkpoints_each_committed_change(monkeypatch, tmp_path):
    uploads = []

    class Checkpoint:
        def upload(self, path: Path):
            uploads.append(path)

    monkeypatch.setattr(cloud_state, "client_from_env", lambda _path: Checkpoint())
    store = StateStore(tmp_path / "attendr.db")
    initial = len(uploads)
    store.was_sent("assignment:1", "hash")
    assert len(uploads) == initial
    store.mark_sent("assignment:1", "hash")
    assert len(uploads) == initial + 1


def test_checkpoint_failure_propagates_before_side_effect(monkeypatch, tmp_path):
    class Checkpoint:
        def upload(self, _path: Path):
            raise CloudStateError("offline")

    monkeypatch.setattr(cloud_state, "client_from_env", lambda _path: Checkpoint())
    with pytest.raises(CloudStateError, match="offline"):
        StateStore(tmp_path / "attendr.db")


def test_upload_recovers_committed_checkpoint_after_lost_response(monkeypatch, tmp_path):
    path = tmp_path / 'state.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sample(value TEXT)')
    remote = {}
    puts = []
    def request(method, url, **kwargs):
        if method == 'PUT':
            puts.append(kwargs['json'])
            remote.update(kwargs['json'], revision=1)
            raise cloud_state.requests.ConnectionError('lost response')
        return Response(remote)
    monkeypatch.setattr(cloud_state.requests, 'request', request)
    client = CloudStateClient('https://state.test', 'secret', 'key', 'a'*32, 0)
    client.upload(path)
    assert client.revision == 1
    assert len(puts) == 1
    assert puts[0]['size'] < path.stat().st_size / 2


def test_upload_retries_uncommitted_service_failure(monkeypatch, tmp_path):
    path = tmp_path / 'state.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sample(value TEXT)')
    puts = []
    def request(method, url, **kwargs):
        if method == 'GET':
            return Response({'revision': 0, 'sha256': None})
        puts.append(kwargs['json'])
        return Response({}, 503) if len(puts) == 1 else Response({'revision': 1})
    monkeypatch.setattr(cloud_state.requests, 'request', request)
    monkeypatch.setattr(cloud_state.time, 'sleep', lambda _: None)
    client = CloudStateClient('https://state.test', 'secret', 'key', 'a'*32, 0)
    client.upload(path)
    assert client.revision == 1
    assert puts[0] == puts[1]

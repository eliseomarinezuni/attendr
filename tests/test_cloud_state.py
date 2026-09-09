import base64
import copy
import json
import sqlite3
from pathlib import Path

import pytest

from academic_assistant import cloud_state
from academic_assistant.cloud_state import (
    CloudStateClient,
    CloudStateError,
    rotate_checkpoint,
)
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


def test_wrong_independent_state_key_cannot_decrypt(monkeypatch, tmp_path):
    remote = {"revision": 0, "sha256": None, "size": 0, "chunks": []}

    def request(method, _url, **kwargs):
        if method == "GET":
            return Response(remote.copy())
        remote.update(kwargs["json"])
        remote["revision"] += 1
        return Response({"revision": remote["revision"]})

    monkeypatch.setattr(cloud_state.requests, "request", request)
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE value(name TEXT)")
        db.execute("INSERT INTO value VALUES('private')")
    CloudStateClient("https://state.test", "auth-secret", "state-key", "a" * 32, 0).upload(source)
    target = tmp_path / "target.db"
    target.write_bytes(b"preserve-me")

    with pytest.raises(CloudStateError, match="ATTENDR_STATE_KEY"):
        CloudStateClient(
            "https://state.test", "auth-secret", "wrong-state-key", "a" * 32, 1
        ).download(target)

    assert target.read_bytes() == b"preserve-me"


def test_missing_state_key_does_not_fall_back_to_authentication_secret(monkeypatch, tmp_path):
    cloud_state._CLIENTS.clear()
    monkeypatch.setenv("ATTENDR_STATE_URL", "https://state.test")
    monkeypatch.setenv("ATTENDR_STATE_SECRET", "worker-auth-secret")
    monkeypatch.setenv("STUDY_SYNC_SECRET", "worker-auth-secret")
    monkeypatch.setenv("ATTENDR_STATE_LEASE", "a" * 32)
    monkeypatch.delenv("ATTENDR_STATE_KEY", raising=False)

    with pytest.raises(CloudStateError, match="ATTENDR_STATE_KEY"):
        cloud_state.client_from_env(tmp_path / "state.db")


def test_equal_authentication_and_encryption_secrets_are_rejected(monkeypatch, tmp_path):
    cloud_state._CLIENTS.clear()
    for name, value in {
        "ATTENDR_STATE_URL": "https://state.test",
        "ATTENDR_STATE_SECRET": "shared-secret",
        "ATTENDR_STATE_KEY": "shared-secret",
        "ATTENDR_STATE_LEASE": "a" * 32,
    }.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(CloudStateError, match="independent"):
        cloud_state.client_from_env(tmp_path / "state.db")


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


class RemoteCheckpoint:
    def __init__(self):
        self.payload = {"revision": 0, "sha256": None, "size": 0, "chunks": []}
        self.fail_puts = False

    def request(self, method, _url, **kwargs):
        if method == "GET":
            return Response(copy.deepcopy(self.payload))
        if self.fail_puts:
            return Response({"error": "unavailable"}, 503)
        assert kwargs["json"]["revision"] == self.payload["revision"]
        self.payload = copy.deepcopy(kwargs["json"])
        self.payload["revision"] += 1
        return Response({"revision": self.payload["revision"]})


def sqlite_fixture(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO sample(value) VALUES('preserved')")


def test_rotation_preserves_database_and_changes_only_encryption_key(monkeypatch, tmp_path):
    remote = RemoteCheckpoint()
    monkeypatch.setattr(cloud_state.requests, "request", remote.request)
    source = tmp_path / "source.db"
    sqlite_fixture(source)
    old = CloudStateClient("https://state.test", "auth", "old-key", "a" * 32, 0)
    old.upload(source)
    before = tmp_path / "before.db"
    assert old.download(before)
    old_payload = copy.deepcopy(remote.payload)

    new = CloudStateClient(
        "https://state.test", "auth", "new-independent-key", "a" * 32, old.revision
    )
    backup = tmp_path / "old-checkpoint.json"
    assert rotate_checkpoint(old, new, backup_path=backup)

    after = tmp_path / "after.db"
    assert new.download(after)
    assert after.read_bytes() == before.read_bytes()
    with sqlite3.connect(after) as db:
        assert db.execute("SELECT value FROM sample").fetchone()[0] == "preserved"
    with pytest.raises(CloudStateError, match="ATTENDR_STATE_KEY"):
        old.download(tmp_path / "wrong-key.db")
    assert json.loads(backup.read_text()) == old_payload
    assert not rotate_checkpoint(old, new, backup_path=backup)


def test_failed_rotation_keeps_remote_old_checkpoint(monkeypatch, tmp_path):
    remote = RemoteCheckpoint()
    monkeypatch.setattr(cloud_state.requests, "request", remote.request)
    monkeypatch.setattr(cloud_state.time, "sleep", lambda _seconds: None)
    source = tmp_path / "source.db"
    sqlite_fixture(source)
    old = CloudStateClient("https://state.test", "auth", "old-key", "a" * 32, 0)
    old.upload(source)
    original_payload = copy.deepcopy(remote.payload)
    remote.fail_puts = True
    new = CloudStateClient("https://state.test", "auth", "new-key", "a" * 32, old.revision)

    with pytest.raises(CloudStateError):
        rotate_checkpoint(old, new, backup_path=tmp_path / "backup.json")

    assert remote.payload == original_payload
    remote.fail_puts = False
    restored = tmp_path / "restored.db"
    assert old.download(restored)
    with sqlite3.connect(restored) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_restore_integrity_failure_does_not_replace_existing_database(monkeypatch, tmp_path):
    remote = RemoteCheckpoint()
    monkeypatch.setattr(cloud_state.requests, "request", remote.request)
    client = CloudStateClient("https://state.test", "auth", "state-key", "a" * 32, 0)
    client._upload_plaintext(b"not a sqlite database")
    target = tmp_path / "target.db"
    sqlite_fixture(target)

    with pytest.raises(CloudStateError, match="integrity check"):
        client.download(target)

    with sqlite3.connect(target) as db:
        assert db.execute("SELECT value FROM sample").fetchone()[0] == "preserved"

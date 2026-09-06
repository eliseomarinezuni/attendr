"""Encrypted SQLite checkpoints for disposable scheduled runners."""

from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_AAD = b"attendr-state-v1"
_CHUNK_BYTES = 192 * 1024
_CLIENTS: dict[Path, "CloudStateClient"] = {}


class CloudStateError(RuntimeError):
    pass


class CloudStateClient:
    def __init__(self, url: str, secret: str, key: str, lease_token: str, revision: int) -> None:
        self.url = url.rstrip("/")
        self.secret = secret
        self.lease_token = lease_token
        self.revision = revision
        if not key:
            raise CloudStateError("ATTENDR_STATE_KEY must be configured")
        raw_key = hashlib.sha256(b"attendr-state-key-v1\0" + key.encode("utf-8")).digest()
        self.cipher = AESGCM(raw_key)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.secret}",
            "X-Attendr-Lease": self.lease_token,
        }

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        try:
            response = requests.request(
                method, f"{self.url}{path}", headers=self.headers, timeout=30, **kwargs
            )
        except requests.RequestException as error:
            raise CloudStateError(f"State checkpoint request failed: {error}") from error
        if not response.ok:
            raise CloudStateError(
                f"State checkpoint {method} {path} failed: HTTP {response.status_code}"
            )
        return response

    def download(self, path: Path) -> bool:
        response = self.request("GET", "/api/state-store")
        payload = response.json()
        self.revision = int(payload["revision"])
        chunks = payload.get("chunks", [])
        if not chunks:
            return False
        encrypted = base64.b64decode("".join(chunks), validate=True)
        if hashlib.sha256(encrypted).hexdigest() != payload["sha256"]:
            raise CloudStateError("State checkpoint checksum mismatch")
        if len(encrypted) != payload["size"]:
            raise CloudStateError("State checkpoint size mismatch")
        if len(encrypted) < 13 or encrypted[0] != 1:
            raise CloudStateError("Unsupported state checkpoint format")
        plaintext = self.cipher.decrypt(encrypted[1:13], encrypted[13:], _AAD)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plaintext)
        path.chmod(0o600)
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise CloudStateError("Downloaded SQLite checkpoint failed integrity check")
        return True

    def upload(self, path: Path) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as snapshot:
            with sqlite3.connect(path) as source, sqlite3.connect(snapshot.name) as target:
                source.backup(target)
            plaintext = Path(snapshot.name).read_bytes()
        nonce = os.urandom(12)
        encrypted = b"\x01" + nonce + self.cipher.encrypt(nonce, plaintext, _AAD)
        encoded = base64.b64encode(encrypted).decode("ascii")
        chunks = [encoded[index : index + _CHUNK_BYTES] for index in range(0, len(encoded), _CHUNK_BYTES)]
        response = self.request(
            "PUT",
            "/api/state-store",
            json={
                "revision": self.revision,
                "sha256": hashlib.sha256(encrypted).hexdigest(),
                "size": len(encrypted),
                "chunks": chunks,
            },
        )
        self.revision = int(response.json()["revision"])


def client_from_env(path: Path) -> CloudStateClient | None:
    url = os.getenv("ATTENDR_STATE_URL", "").strip()
    if not url:
        return None
    resolved = path.resolve()
    if resolved not in _CLIENTS:
        required = {
            name: os.getenv(name, "").strip()
            for name in ("ATTENDR_STATE_SECRET", "ATTENDR_STATE_KEY", "ATTENDR_STATE_LEASE")
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise CloudStateError("Missing cloud state settings: " + ", ".join(missing))
        try:
            revision = int(os.getenv("ATTENDR_STATE_REVISION", "0"))
        except ValueError as error:
            raise CloudStateError("ATTENDR_STATE_REVISION must be an integer") from error
        _CLIENTS[resolved] = CloudStateClient(
            url, required["ATTENDR_STATE_SECRET"], required["ATTENDR_STATE_KEY"],
            required["ATTENDR_STATE_LEASE"], revision,
        )
    return _CLIENTS[resolved]

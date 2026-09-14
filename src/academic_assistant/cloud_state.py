"""Encrypted SQLite checkpoints for disposable scheduled runners."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any

import requests
from cryptography.exceptions import InvalidTag
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
            try:
                detail = str(response.json().get("error", ""))
            except (ValueError, AttributeError):
                detail = ""
            raise CloudStateError(
                f"State checkpoint {method} {path} failed: HTTP {response.status_code}"
                + (f" ({detail})" if detail else "")
            )
        return response

    def download(self, path: Path) -> bool:
        payload = self.request("GET", "/api/state-store").json()
        self.revision = int(payload["revision"])
        if not payload.get("chunks", []):
            return False
        plaintext = self.decode_payload(payload)
        self._write_verified_database(path, plaintext)
        return True

    def decode_payload(self, payload: dict[str, Any]) -> bytes:
        """Authenticate and decrypt one checkpoint payload without changing remote state."""
        try:
            chunks = payload["chunks"]
            encrypted = base64.b64decode("".join(chunks), validate=True)
            expected_hash = payload["sha256"]
            expected_size = int(payload["size"])
        except (KeyError, TypeError, ValueError, binascii.Error) as error:
            raise CloudStateError("State checkpoint payload is invalid") from error
        if hashlib.sha256(encrypted).hexdigest() != expected_hash:
            raise CloudStateError("State checkpoint checksum mismatch")
        if len(encrypted) != expected_size:
            raise CloudStateError("State checkpoint size mismatch")
        if len(encrypted) < 13 or encrypted[0] not in (1, 2):
            raise CloudStateError("Unsupported state checkpoint format")
        try:
            plaintext = self.cipher.decrypt(encrypted[1:13], encrypted[13:], _AAD)
            return zlib.decompress(plaintext) if encrypted[0] == 2 else plaintext
        except InvalidTag as error:
            raise CloudStateError(
                "State checkpoint could not be decrypted with ATTENDR_STATE_KEY"
            ) from error
        except zlib.error as error:
            raise CloudStateError("State checkpoint compression is invalid") from error

    def restore_payload(self, payload: dict[str, Any], path: Path) -> None:
        """Decrypt and integrity-check an already fetched checkpoint payload."""
        self._write_verified_database(path, self.decode_payload(payload))

    @staticmethod
    def _verify_database(path: Path) -> None:
        try:
            with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise CloudStateError("Downloaded SQLite checkpoint failed integrity check")
        except sqlite3.Error as error:
            raise CloudStateError("Downloaded SQLite checkpoint failed integrity check") from error

    @classmethod
    def _write_verified_database(cls, path: Path, plaintext: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(raw_path)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(plaintext)
            temporary.chmod(0o600)
            cls._verify_database(temporary)
            temporary.replace(path)
        except (OSError, sqlite3.Error):
            temporary.unlink(missing_ok=True)
            raise
        except CloudStateError:
            temporary.unlink(missing_ok=True)
            raise

    def upload(self, path: Path) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as snapshot:
            with sqlite3.connect(path) as source, sqlite3.connect(snapshot.name) as target:
                source.backup(target)
            plaintext = Path(snapshot.name).read_bytes()
        self._upload_plaintext(plaintext)

    def upload_exact(self, path: Path) -> None:
        """Upload the exact verified SQLite bytes, used only for key rotation."""
        self._verify_database(path)
        self._upload_plaintext(path.read_bytes(), verify_ciphertext=True)

    def _upload_plaintext(self, plaintext: bytes, *, verify_ciphertext: bool = False) -> None:
        nonce = os.urandom(12)
        encrypted = b"\x02" + nonce + self.cipher.encrypt(nonce, zlib.compress(plaintext), _AAD)
        encoded = base64.b64encode(encrypted).decode("ascii")
        chunks = [
            encoded[index : index + _CHUNK_BYTES] for index in range(0, len(encoded), _CHUNK_BYTES)
        ]
        payload = {
            "revision": self.revision,
            "sha256": hashlib.sha256(encrypted).hexdigest(),
            "size": len(encrypted),
            "chunks": chunks,
        }
        if verify_ciphertext and self.decode_payload(payload) != plaintext:
            raise CloudStateError("Locally verified rotated checkpoint did not round-trip")
        self._upload_payload(payload)

    def _upload_payload(self, payload: dict[str, Any]) -> None:
        for attempt in range(3):
            try:
                response = self.request("PUT", "/api/state-store", json=payload)
                self.revision = int(response.json()["revision"])
                return
            except CloudStateError:
                # A lost response may hide a successful commit. Verify before retrying.
                remote = self.request("GET", "/api/state-store").json()
                if (
                    remote.get("sha256") == payload["sha256"]
                    and remote["revision"] == self.revision + 1
                ):
                    self.revision = int(remote["revision"])
                    return
                if remote["revision"] != self.revision or attempt == 2:
                    raise
                time.sleep(attempt + 1)


def rotate_checkpoint(
    old_client: CloudStateClient,
    new_client: CloudStateClient,
    *,
    backup_path: Path,
) -> bool:
    """Rotate one leased remote checkpoint; return False when already rotated."""
    payload = old_client.request("GET", "/api/state-store").json()
    try:
        revision = int(payload["revision"])
    except (KeyError, TypeError, ValueError) as error:
        raise CloudStateError("State checkpoint payload is invalid") from error
    if not payload.get("chunks"):
        raise CloudStateError("No encrypted state checkpoint exists to rotate")
    old_client.revision = new_client.revision = revision

    with tempfile.TemporaryDirectory(prefix="attendr-state-rotation-") as directory:
        temporary = Path(directory)
        already_rotated = temporary / "already-rotated.db"
        try:
            new_client.restore_payload(payload, already_rotated)
        except CloudStateError:
            pass
        else:
            return False

        original = temporary / "original.db"
        old_client.restore_payload(payload, original)
        _save_encrypted_backup(backup_path, payload)

        new_client.upload_exact(original)
        verified = temporary / "verified.db"
        if not new_client.download(verified):
            raise CloudStateError("Rotated checkpoint disappeared during verification")
        if (
            hashlib.sha256(verified.read_bytes()).digest()
            != hashlib.sha256(original.read_bytes()).digest()
        ):
            raise CloudStateError("Rotated checkpoint does not preserve the SQLite database")
    return True


def _save_encrypted_backup(path: Path, payload: dict[str, Any]) -> None:
    """Persist the old encrypted payload without ever overwriting a recovery copy."""
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CloudStateError("Existing rotation backup is unreadable") from error
        if existing != payload:
            raise CloudStateError("Existing rotation backup belongs to another checkpoint")
        return
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(serialized)
    except OSError:
        path.unlink(missing_ok=True)
        raise


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
        if required["ATTENDR_STATE_SECRET"] == required["ATTENDR_STATE_KEY"]:
            raise CloudStateError("ATTENDR_STATE_KEY must be independent from ATTENDR_STATE_SECRET")
        try:
            revision = int(os.getenv("ATTENDR_STATE_REVISION", "0"))
        except ValueError as error:
            raise CloudStateError("ATTENDR_STATE_REVISION must be an integer") from error
        _CLIENTS[resolved] = CloudStateClient(
            url,
            required["ATTENDR_STATE_SECRET"],
            required["ATTENDR_STATE_KEY"],
            required["ATTENDR_STATE_LEASE"],
            revision,
        )
    return _CLIENTS[resolved]

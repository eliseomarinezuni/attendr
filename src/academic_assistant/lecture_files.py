"""Bounded large lecture downloads and media-free PowerPoint extraction."""

from __future__ import annotations

import ipaddress
import os
import posixpath
import socket
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool

SMALL_FILE_BYTES = 25 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_TOTAL_XML_BYTES = 64 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000


class LectureDownloadError(ValueError):
    def __init__(self, status=None, category="transport"):
        messages = {
            "transport": "Lecture download failed",
            "invalid_url": "Lecture download rejected: invalid URL",
            "resolution": "Lecture download rejected: destination resolution failed",
            "unsafe_destination": "Lecture download rejected: unsafe destination",
        }
        super().__init__(messages[category])
        self.status = status
        self.category = category


@dataclass(frozen=True, slots=True)
class DownloadDestination:
    hostname: str
    port: int
    addresses: tuple[str, ...]


def validate_download_destination(url: str) -> DownloadDestination:
    """Resolve an HTTPS URL and reject it if any possible destination is non-public."""
    try:
        parsed = urlparse(url)
        port = parsed.port or 443
    except ValueError:
        raise LectureDownloadError(category="invalid_url") from None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise LectureDownloadError(category="invalid_url")
    try:
        resolved = socket.getaddrinfo(
            parsed.hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    except OSError:
        raise LectureDownloadError(category="resolution") from None
    addresses = tuple(dict.fromkeys(str(item[4][0]) for item in resolved if item[4]))
    if not addresses:
        raise LectureDownloadError(category="resolution")
    try:
        unsafe = any(
            not ipaddress.ip_address(address.split("%", 1)[0]).is_global for address in addresses
        )
    except ValueError:
        raise LectureDownloadError(category="resolution") from None
    if unsafe:
        raise LectureDownloadError(category="unsafe_destination")
    return DownloadDestination(parsed.hostname.rstrip("."), port, addresses)


class _PinnedHTTPSConnection(HTTPSConnection):
    def __init__(self, host, *args, pinned_address, **kwargs):
        # Connect to the validated address, while keeping the requested hostname
        # for SNI and certificate verification.
        hostname = host.rstrip(".")
        kwargs["server_hostname"] = hostname
        kwargs["assert_hostname"] = hostname
        super().__init__(host, *args, **kwargs)
        self._dns_host = pinned_address


class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection  # pyright: ignore[reportAssignmentType]


class _PinnedHTTPSAdapter(HTTPAdapter):
    def __init__(self, destination: DownloadDestination, address_index: int = 0):
        self.destination = destination
        self.pinned_address = destination.addresses[address_index % len(destination.addresses)]
        super().__init__(max_retries=0)

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        if proxies:
            raise LectureDownloadError(category="unsafe_destination")
        host, pool_options = self.build_connection_pool_key_attributes(request, verify, cert)
        if host["host"].rstrip(".") != self.destination.hostname or host["port"] not in (
            None,
            self.destination.port,
        ):
            raise LectureDownloadError(category="unsafe_destination")
        return _PinnedHTTPSConnectionPool(
            host["host"],
            self.destination.port,
            pinned_address=self.pinned_address,
            **pool_options,
        )


def _pinned_session(destination: DownloadDestination, address_index: int):
    session = requests.Session()
    session.trust_env = False
    session.mount("https://", _PinnedHTTPSAdapter(destination, address_index))
    return session


def lecture_file_limit() -> int:
    value = int(os.getenv("LECTURE_MAX_FILE_MB", "768"))
    if not 1 <= value <= 1024:
        raise ValueError("LECTURE_MAX_FILE_MB must be between 1 and 1024")
    return value * 1024 * 1024


def stream_download(url: str, destination: Path, limit: int) -> None:
    """Use Canvas's signed file URL; never forward the API bearer token."""
    deadline = time.monotonic() + 300
    for attempt in range(3):
        try:
            current = url
            for redirect in range(6):
                if time.monotonic() > deadline:
                    raise ValueError("Lecture download exceeds time limit")
                safe_destination = validate_download_destination(current)
                with _pinned_session(safe_destination, attempt) as session:
                    with session.get(
                        current,
                        stream=True,
                        timeout=(10, 30),
                        allow_redirects=False,
                    ) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            current = urljoin(current, response.headers.get("Location", ""))
                            continue
                        response.raise_for_status()
                        if int(response.headers.get("Content-Length", "0")) > limit:
                            raise ValueError("Lecture download exceeds byte limit")
                        size = 0
                        with destination.open("wb") as output:
                            for block in response.iter_content(chunk_size=1024 * 1024):
                                size += len(block)
                                if size > limit or time.monotonic() > deadline:
                                    raise ValueError("Lecture download exceeds byte or time limit")
                                output.write(block)
                        return
            raise ValueError("Too many lecture download redirects")
        except requests.RequestException as error:
            destination.unlink(missing_ok=True)
            status = error.response.status_code if error.response is not None else None
            if attempt == 2 or (status is not None and status < 500 and status != 429):
                raise LectureDownloadError(status) from None
            time.sleep(2**attempt)
        except Exception:
            destination.unlink(missing_ok=True)
            raise


def powerpoint_slide_text(path: Path) -> list[tuple[int, str]]:
    """Read only ordered slide/notes XML; never inflate embedded media."""
    ns = {
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    }
    rid = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    total = 0
    with zipfile.ZipFile(path) as archive:

        def xml(name):
            nonlocal total
            info = archive.getinfo(name)
            total += info.file_size
            if info.file_size > MAX_XML_BYTES or total > MAX_TOTAL_XML_BYTES:
                raise ValueError("PowerPoint XML exceeds extraction limit")
            return ET.fromstring(archive.read(info))

        def relationships(part):
            name = posixpath.join(
                posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels"
            )
            if name not in archive.namelist():
                return {}
            result = {}
            for item in xml(name):
                if item.get("TargetMode") == "External":
                    continue
                target = item.get("Target", "")
                target = (
                    target.lstrip("/")
                    if target.startswith("/")
                    else posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
                )
                if not target.startswith("ppt/"):
                    raise ValueError("Invalid PowerPoint relationship")
                result[item.get("Id")] = (target, item.get("Type", ""))
            return result

        presentation = xml("ppt/presentation.xml")
        rels = relationships("ppt/presentation.xml")
        result, chars = [], 0
        for number, slide in enumerate(presentation.findall("p:sldIdLst/p:sldId", ns), 1):
            target, _ = rels[slide.get(rid)]
            root = xml(target)
            parts = []
            for paragraph in root.findall(".//a:p", ns):
                text = "".join(t.text or "" for t in paragraph.findall(".//a:t", ns)).strip()
                if text:
                    parts.append(text)
            for note, kind in relationships(target).values():
                if not kind.endswith("/notesSlide"):
                    continue
                notes = xml(note)
                for shape in notes.findall(".//p:sp", ns):
                    placeholder = shape.find(".//p:ph", ns)
                    if placeholder is not None and placeholder.get("type") in (
                        "sldNum",
                        "hdr",
                        "ftr",
                        "dt",
                        "sldImg",
                    ):
                        continue
                    text = " ".join(t.text or "" for t in shape.findall(".//a:t", ns)).strip()
                    if text:
                        parts.append("Speaker notes: " + text)
            text = "\n".join(parts)
            chars += len(text)
            if chars > MAX_TEXT_CHARS:
                raise ValueError("PowerPoint text exceeds extraction limit")
            if text:
                result.append((number, text))
        return result

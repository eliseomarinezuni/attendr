from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.setup_lecture_summaries import (
    CHANNEL_ACCESS,
    VIEW_CHANNEL,
    setup,
)


class FakeDiscord:
    def __init__(self, channels: list[dict[str, object]]) -> None:
        self.channels = channels
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> SimpleNamespace:
        self.calls.append((method, url, kwargs))
        if url.endswith("/channels") and method == "GET":
            value: object = self.channels
        elif url.endswith("/users/@me"):
            value = {"id": "bot-id"}
        elif method in {"POST", "PATCH"}:
            value = {"id": "channel-id"}
        else:
            raise AssertionError((method, url))
        return SimpleNamespace(ok=True, status_code=200, json=lambda: value)


def test_creates_private_summary_channel_with_owner_and_bot_access():
    discord = FakeDiscord([])

    channel_id = setup("private-token", "guild-id", "owner-id", session=discord)

    assert channel_id == "channel-id"
    method, _url, request = discord.calls[-1]
    assert method == "POST"
    overwrites = request["json"]["permission_overwrites"]
    assert overwrites == [
        {"id": "guild-id", "type": 0, "deny": str(VIEW_CHANNEL), "allow": "0"},
        {
            "id": "owner-id",
            "type": 1,
            "allow": str(CHANNEL_ACCESS),
            "deny": "0",
        },
        {
            "id": "bot-id",
            "type": 1,
            "allow": str(CHANNEL_ACCESS),
            "deny": "0",
        },
    ]
    assert "private-token" not in str(request["json"])


def test_existing_channel_is_hardened_idempotently():
    discord = FakeDiscord([{"id": "existing-id", "name": "lecture-summaries", "type": 0}])

    setup("private-token", "guild-id", "owner-id", session=discord)

    method, url, _request = discord.calls[-1]
    assert method == "PATCH"
    assert url.endswith("/channels/existing-id")


def test_duplicate_channels_fail_safely_without_mutation():
    discord = FakeDiscord(
        [
            {"id": "one", "name": "lecture-summaries", "type": 0},
            {"id": "two", "name": "lecture-summaries", "type": 0},
        ]
    )

    with pytest.raises(RuntimeError, match="Multiple #lecture-summaries"):
        setup("private-token", "guild-id", "owner-id", session=discord)

    assert [method for method, _url, _request in discord.calls] == ["GET"]

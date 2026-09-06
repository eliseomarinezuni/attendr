"""Split Discord embeds into independently recoverable messages."""

from __future__ import annotations
from copy import deepcopy
from typing import Any, Mapping


def discord_messages(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = deepcopy(dict(payload))
    if len(value.get("content", "")) > 2000:
        raise ValueError("Discord content exceeds 2000 characters")
    embeds = value.pop("embeds", [])
    messages: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    total = 0
    for embed in embeds:
        # Preserve complete field text by splitting it, including spoiler wrappers.
        fields = []
        for field in embed.get("fields", []):
            text = field["value"]
            spoiler = text.startswith("||") and text.endswith("||")
            if spoiler:
                text = text[2:-2]
            limit = 1020 if spoiler else 1024
            for offset in range(0, max(1, len(text)), limit):
                piece = text[offset : offset + limit] or "\u200b"
                fields.append(
                    {
                        **field,
                        "name": field["name"][:256],
                        "value": f"||{piece}||" if spoiler else piece,
                    }
                )
        if fields:
            embed["fields"] = fields
        if (
            len(fields) > 25
            or len(embed.get("title", "")) > 256
            or len(embed.get("description", "")) > 4096
        ):
            raise ValueError("Discord embed exceeds a per-embed limit")
        footer = embed.get("footer", {}).get("text", "")
        author = embed.get("author", {}).get("name", "")
        if len(footer) > 2048 or len(author) > 256:
            raise ValueError("Discord footer or author exceeds its limit")
        size = (
            len(embed.get("title", ""))
            + len(embed.get("description", ""))
            + len(footer)
            + len(author)
        )
        size += sum(len(field["name"]) + len(field["value"]) for field in fields)
        if size > 6000:
            raise ValueError("Discord embed exceeds 6000 characters")
        if current and (total + size > 6000 or len(current) == 10):
            messages.append({**value, "embeds": current})
            current, total = [], 0
        current.append(embed)
        total += size
    if current or not messages:
        messages.append({**value, **({"embeds": current} if current else {})})
    return messages

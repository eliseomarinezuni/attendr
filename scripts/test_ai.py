#!/usr/bin/env python3
"""Generate a Gemini quiz from a topic or PDF and post it to Discord."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from academic_assistant import (
    AIAssistant,
    AIConfigurationError,
    AIInputError,
    AIProviderError,
    DiscordConfigurationError,
    DiscordNotificationError,
    DiscordNotifier,
    PDFTextChunk,
    extract_pdf_text_chunks,
    send_quiz_to_discord,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an active-recall quiz and send it to Discord."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--topic", help='Lecture topic, such as "Binary Search Trees".')
    source.add_argument("--pdf", type=Path, help="Path to a syllabus or lecture PDF.")
    parser.add_argument(
        "--questions",
        type=int,
        choices=(3, 4, 5),
        default=3,
        help="Number of questions (default: 3).",
    )
    parser.add_argument("--model", help="Temporarily override GEMINI_MODEL.")
    parser.add_argument(
        "--extract-syllabus",
        action="store_true",
        help="Also print structured schedule JSON for a syllabus PDF.",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="List Flash models available to the configured API key.",
    )
    parser.add_argument(
        "--check-connection",
        action="store_true",
        help="Make one plain-text request to verify the key and configured model.",
    )
    return parser.parse_args()


def resolve_source(
    args: argparse.Namespace,
) -> tuple[str, Literal["pdf", "topic"], Sequence[PDFTextChunk] | None]:
    if args.pdf:
        chunks = extract_pdf_text_chunks(args.pdf)
        return args.pdf.stem.replace("_", " "), "pdf", chunks
    if args.topic:
        return args.topic.strip(), "topic", None

    entered = input("Enter a lecture topic or PDF path: ").strip()
    if not entered:
        raise AIInputError("A lecture topic or PDF path is required.")
    possible_path = Path(entered).expanduser()
    if possible_path.suffix.casefold() == ".pdf":
        chunks = extract_pdf_text_chunks(possible_path)
        return possible_path.stem.replace("_", " "), "pdf", chunks
    return entered, "topic", None


def main() -> int:
    args = parse_args()
    try:
        if args.list_models:
            assistant = AIAssistant.from_env(
                PROJECT_ROOT / ".env", model_override=args.model
            )
            models = assistant.list_available_flash_models()
            print("Available Flash models:")
            for model in models:
                print(f"  - {model}")
            return 0 if models else 1
        if args.check_connection:
            assistant = AIAssistant.from_env(
                PROJECT_ROOT / ".env", model_override=args.model
            )
            assistant.check_connection()
            print(f"Gemini connection succeeded with {assistant.model}.")
            return 0

        label, source_kind, chunks = resolve_source(args)
        assistant = AIAssistant.from_env(
            PROJECT_ROOT / ".env", model_override=args.model
        )
        notifier = DiscordNotifier.from_env(PROJECT_ROOT / ".env")

        if source_kind == "pdf":
            assert chunks is not None
            print(f"Extracted {len(chunks)} clean text chunk(s) from the PDF.")
            if args.extract_syllabus:
                schedule = assistant.extract_syllabus_schedule(chunks)
                print("Structured syllabus schedule:")
                print(json.dumps(schedule, indent=2, ensure_ascii=False))
            quiz_source = "\n\n".join(chunk.text for chunk in chunks)
        else:
            quiz_source = label

        quiz = assistant.generate_quiz(quiz_source, args.questions)
        sent = send_quiz_to_discord(
            notifier,
            label,
            quiz,
            force=True,
            event_key="module-4-smoke-test",
        )
    except (
        AIConfigurationError,
        AIInputError,
        AIProviderError,
        DiscordConfigurationError,
        DiscordNotificationError,
    ) as error:
        print(f"AI test failed: {error}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nAI test cancelled.", file=sys.stderr)
        return 130

    if sent:
        print(f"Sent {len(quiz)} quiz questions to Discord with hidden answers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from academic_assistant.ai_assistant import (
    AIAssistant,
    AIProviderError,
    GeneratedLectureSummary,
    PDFTextChunk,
)
from academic_assistant.ai_efficiency import AI_USAGE
from academic_assistant.canvas_client import LectureMaterial
from academic_assistant.course_schedule import ClassSession
from academic_assistant.delivery import discord_messages
from academic_assistant.lecture_content import build_lecture_content_bundle
from academic_assistant.lecture_quiz import LectureQuizRunner
from academic_assistant.lecture_summary import lecture_summary_discord_payload
from academic_assistant.notifier import DiscordNotificationError, DiscordNotifier
from academic_assistant.state_store import StateStore

UTC = timezone.utc


def point(text: str, *sources: str, quote: str | None = None) -> dict[str, object]:
    return {"text": text, "source_ids": list(sources), "source_quote": quote}


def summary(source_id: str = "S1") -> dict[str, object]:
    base = point("A tree organizes nodes through parent-child relationships.", source_id)
    return {
        "topic": "Trees and traversal",
        "tldr": [base],
        "key_concepts": [base],
        "teaching_sections": [base],
        "examples": [],
        "algorithms_or_code": [],
        "formulas": [],
        "common_mistakes": [],
        "connections": [],
        "learning_objectives": [base],
        "source_refs": [source_id],
    }


def quiz() -> list[dict[str, object]]:
    return [
        {
            "question_type": "multiple_choice",
            "question": f"Question {number}?",
            "options": ["One", "Two", "Three", "Four"],
            "correct_answer": "A",
            "explanation": "The first option follows from the material.",
        }
        for number in (1, 2)
    ] + [
        {
            "question_type": "short_answer",
            "question": "Explain the central idea.",
            "options": [],
            "correct_answer": "Nodes form parent-child relationships.",
            "explanation": "This describes the hierarchy.",
        }
    ]


def session() -> ClassSession:
    return ClassSession(
        course_key="csc",
        course_name="Data Structures",
        course_match=("data structures",),
        weekday=0,
        start=time(13),
        end=time(14),
        activity="lecture",
    )


def material(path: Path, *, uid: str = "file:1", digest: str | None = None) -> LectureMaterial:
    return LectureMaterial(
        uid=uid,
        source_id=uid.rsplit(":", 1)[-1],
        course_id=10,
        course_name="Data Structures",
        title=path.stem,
        content_type="text/plain",
        local_path=path,
        content_sha256=digest or sha256(path.read_bytes()).hexdigest(),
        updated_at=datetime(2026, 9, 14, tzinfo=UTC),
        html_url="https://canvas.example.edu/courses/10/files/1",
        module_name="Week 2 Trees",
        module_position=2,
        item_position=1,
        module_id="20",
        item_id="30",
    )


class FakeModels:
    def __init__(self, response: dict[str, object]):
        self.response = response
        self.calls: list[dict[str, object]] = []

    def generate_content(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(text=json.dumps([provider_summary(self.response)]))


def provider_summary(value: dict[str, object]) -> dict[str, object]:
    if "points" in value:
        return value
    points: list[dict[str, object]] = []
    for section in (
        "tldr",
        "key_concepts",
        "teaching_sections",
        "examples",
        "algorithms_or_code",
        "formulas",
        "common_mistakes",
        "connections",
        "learning_objectives",
    ):
        section_points = value[section]
        assert isinstance(section_points, list)
        for point_value in section_points:
            assert isinstance(point_value, dict)
            points.append({"section": section, **point_value})
    return {
        "topic": value["topic"],
        "points": points,
        "source_refs": value["source_refs"],
    }


class FakeClient:
    def __init__(self, response: dict[str, object]):
        self.models = FakeModels(response)


class FakeAI:
    max_input_chars = 120_000

    def __init__(self) -> None:
        self.summary_calls = 0
        self.quiz_calls = 0

    def model_for_task(self, task: str) -> str:
        assert task == "lecture_summary"
        return "gemini-test"

    def generate_lecture_summary(
        self, _text: str, *, source_texts: dict[str, str]
    ) -> dict[str, object]:
        self.summary_calls += 1
        assert source_texts == {"S1": "A tree organizes nodes through parent-child relationships."}
        return summary()

    def generate_hybrid_quiz(self, _text: str) -> list[dict[str, object]]:
        self.quiz_calls += 1
        return quiz()


class FakeNotifier:
    def __init__(self, store: StateStore, *, fail_summary_once: bool = False) -> None:
        self.store = store
        self.fail_summary_once = fail_summary_once
        self.calls: list[tuple[str, str]] = []

    def send_custom_notification(
        self,
        event_key: str,
        _payload: dict[str, object],
        *,
        force: bool = False,
        destination: str = "announcements",
        fixed_fingerprint: str | None = None,
    ) -> bool:
        self.calls.append((event_key, destination))
        if destination == "lecture_summaries" and self.fail_summary_once:
            self.fail_summary_once = False
            raise DiscordNotificationError("temporary Discord failure")
        if not force:
            self.store.mark_sent(
                f"{destination}:custom:{event_key}", fixed_fingerprint or "generated"
            )
        return True


def runner(
    tmp_path: Path,
    *,
    fail_summary_once: bool = False,
    summary_generation_limit: int | None = None,
):
    source = tmp_path / "trees.txt"
    source.write_text(
        "A tree organizes nodes through parent-child relationships.", encoding="utf-8"
    )
    selected = material(source)
    ended_at = datetime(2026, 9, 14, 14, tzinfo=UTC)
    schedule = Mock()
    schedule.ended_lecture_sessions.return_value = [(session(), ended_at)]
    schedule.excluded_course_patterns = ()
    schedule.lecture_module_policy = Mock(return_value=True)
    canvas = Mock()
    canvas.download_lecture_materials.return_value = SimpleNamespace(
        materials=(selected,), warnings=()
    )
    store = StateStore(tmp_path / "attendr.db")
    ai = FakeAI()
    notifier = FakeNotifier(store, fail_summary_once=fail_summary_once)
    result = LectureQuizRunner(
        canvas,
        ai,
        notifier,
        schedule,
        materials_directory=tmp_path,
        state_path=store.path,
        summary_generation_limit=summary_generation_limit,
    )
    result._select_materials = Mock(return_value=(selected,))
    return result, canvas, ai, notifier, selected, ended_at


def test_bundle_preserves_multiple_sources_and_deduplicates_identical_content(tmp_path):
    first = tmp_path / "slides.txt"
    first.write_text("[Slide 1]\nTrees have roots.", encoding="utf-8")
    second = tmp_path / "notes.txt"
    second.write_text("[Page 1]\nTraversal visits nodes.", encoding="utf-8")
    duplicate = tmp_path / "slides-copy.txt"
    duplicate.write_bytes(first.read_bytes())
    selected = (
        material(first, uid="file:1"),
        material(second, uid="page:2"),
        material(duplicate, uid="file:3", digest=sha256(first.read_bytes()).hexdigest()),
    )

    bundle = build_lecture_content_bundle(
        session(), datetime(2026, 9, 14, 14, tzinfo=UTC), selected
    )

    assert [source.reference_id for source in bundle.sources] == ["S1", "S2"]
    assert bundle.sources[0].location == "slides 1–1"
    assert bundle.sources[1].location == "pages 1–1"
    assert bundle.combined_text.count("BEGIN UNTRUSTED COURSE MATERIAL") == 2


def test_bundle_attributes_pdf_powerpoint_and_canvas_page_sources(tmp_path):
    pdf = tmp_path / "lecture.pdf"
    pptx = tmp_path / "lecture.pptx"
    page = tmp_path / "page.html"
    pdf.write_bytes(b"synthetic-pdf")
    pptx.write_bytes(b"synthetic-pptx")
    page.write_text("<h1>Trees</h1><p>Pages can add context.</p>", encoding="utf-8")
    with (
        patch(
            "academic_assistant.lecture_content.extract_pdf_text_chunks",
            return_value=(PDFTextChunk(1, 1, 2, "PDF concepts"),),
        ),
        patch(
            "academic_assistant.lecture_content.extract_powerpoint_text_chunks",
            return_value=(PDFTextChunk(1, 3, 4, "Slide concepts"),),
        ),
    ):
        bundle = build_lecture_content_bundle(
            session(),
            datetime(2026, 9, 14, 14, tzinfo=UTC),
            (
                material(pdf, uid="file:pdf"),
                material(pptx, uid="file:pptx"),
                material(page, uid="page:html"),
            ),
        )

    assert [source.extraction_method for source in bundle.sources] == [
        "pdf_text",
        "powerpoint_text",
        "canvas_page_text",
    ]
    assert [source.location for source in bundle.sources] == ["pages 1–2", "slides 3–4", None]


def test_unreadable_source_is_reported_without_discarding_readable_source(tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"\xff\xfe")
    good = tmp_path / "good.txt"
    good.write_text("Readable lecture material.", encoding="utf-8")

    bundle = build_lecture_content_bundle(
        session(),
        datetime(2026, 9, 14, 14, tzinfo=UTC),
        (material(bad, uid="file:bad"), material(good, uid="file:good")),
    )

    assert [source.canvas_uid for source in bundle.sources] == ["file:good"]
    assert bundle.warnings == ("Skipped unreadable lecture source file:bad (UnicodeDecodeError).",)


def test_gemini_summary_is_structured_grounded_and_low_temperature():
    result = summary()
    result["tldr"] = [
        point(
            "A tree organizes nodes through parent-child relationships.",
            "S1",
            quote="parent-child relationships",
        )
    ]
    client = FakeClient(result)
    assistant = AIAssistant("test-key", client=client)

    generated = assistant.generate_lecture_summary(
        "SOURCE S1\nA tree organizes nodes through parent-child relationships.",
        source_texts={"S1": "A tree organizes nodes through parent-child relationships."},
    )

    assert generated["source_refs"] == ["S1"]
    call = client.models.calls[0]
    assert call["model"] == assistant.model_for_task("lecture_summary")
    assert call["config"].temperature == 0.2
    assert call["config"].response_schema == list[GeneratedLectureSummary]


def test_flat_provider_summary_requires_all_core_sections():
    assistant = AIAssistant(
        "test-key",
        client=FakeClient(
            {
                "topic": "Trees",
                "points": [],
                "source_refs": ["S1"],
            }
        ),
    )

    with pytest.raises(AIProviderError, match="invalid structured data"):
        assistant.generate_lecture_summary(
            "SOURCE S1\nTrees have nodes.",
            source_texts={"S1": "Trees have nodes."},
        )


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda value: value.update(source_refs=["S2"]),
            "unknown lecture-summary source",
        ),
        (
            lambda value: value["tldr"].__setitem__(
                0, point("Trees are hierarchical.", "S1", quote="invented quotation")
            ),
            "unsupported lecture-summary quote",
        ),
        (
            lambda value: value["tldr"].__setitem__(0, point("Traversal runs in O(1).", "S1")),
            "unsupported lecture-summary complexity claim",
        ),
        (
            lambda value: value["tldr"].__setitem__(
                0, point("The deadline is September 30, 2026.", "S1")
            ),
            "unsupported lecture-summary date claim",
        ),
        (
            lambda value: value.update(
                formulas=[point("The formula is x = 42", "S1", quote="parent-child")]
            ),
            "unsupported lecture-summary formula",
        ),
        (
            lambda value: value["tldr"].__setitem__(
                0, point("The professor emphasized tree rotations.", "S1")
            ),
            "unsupported spoken lecture content",
        ),
        (
            lambda value: value["tldr"].__setitem__(
                0, point("Tree rotations are likely to appear on the exam.", "S1")
            ),
            "unsupported exam importance",
        ),
    ],
)
def test_grounding_rejects_fabricated_attribution(mutate, message):
    result = summary()
    mutate(result)
    assistant = AIAssistant("test-key", client=FakeClient(result))

    with pytest.raises(AIProviderError, match=message):
        assistant.generate_lecture_summary(
            "SOURCE S1\nA tree organizes nodes through parent-child relationships.",
            source_texts={"S1": "A tree organizes nodes through parent-child relationships."},
        )


def test_discord_payload_preserves_code_blocks_citations_and_source_section(tmp_path):
    source = tmp_path / "trees.txt"
    source.write_text(
        "A tree organizes nodes through parent-child relationships.", encoding="utf-8"
    )
    bundle = build_lecture_content_bundle(
        session(), datetime(2026, 9, 14, 14, tzinfo=UTC), (material(source),)
    )
    value = summary()
    value["algorithms_or_code"] = [point("```python\nvisit(node)\n```", "S1")]
    value["examples"] = [point("Do not fetch https://untrusted.example/resource.", "S1")]

    payload = lecture_summary_discord_payload(bundle, value)

    assert payload["allowed_mentions"] == {"parse": []}
    descriptions = "\n".join(embed["description"] for embed in payload["embeds"])
    assert "```python\nvisit(node)\n```" in descriptions
    assert "https://untrusted.example" not in descriptions
    assert "**[S1]**" in descriptions
    assert "Sources" in [embed["title"] for embed in payload["embeds"]]
    assert bundle.sources[0].canvas_uid in descriptions
    assert "canvas.example.edu" not in json.dumps(payload)


def test_long_summary_splits_at_complete_points_and_disables_mentions(tmp_path):
    source = tmp_path / "trees.txt"
    source.write_text(
        "A tree organizes nodes through parent-child relationships.", encoding="utf-8"
    )
    bundle = build_lecture_content_bundle(
        session(), datetime(2026, 9, 14, 14, tzinfo=UTC), (material(source),)
    )
    value = summary()
    value["teaching_sections"] = [
        point(f"Concept {index}: " + "careful explanation " * 30, "S1") for index in range(10)
    ]

    messages = discord_messages(lecture_summary_discord_payload(bundle, value))

    assert len(messages) > 1
    assert all(message["allowed_mentions"] == {"parse": []} for message in messages)
    descriptions = [
        embed["description"] for message in messages for embed in message.get("embeds", [])
    ]
    rendered = "\n".join(descriptions)
    assert all(rendered.count(f"Concept {index}:") == 1 for index in range(10))


def test_summary_delivery_never_falls_back_to_general_webhook(tmp_path):
    http = Mock()
    notifier = DiscordNotifier(
        "https://discord.com/api/webhooks/1/test-value",
        state_path=tmp_path / "state.db",
        session=http,
    )

    with pytest.raises(DiscordNotificationError, match="dedicated lecture-summaries"):
        notifier.send_custom_notification(
            "lecture-summary:session",
            {"content": "summary", "allowed_mentions": {"parse": []}},
            destination="lecture_summaries",
        )

    http.post.assert_not_called()


def test_just_ended_shared_run_downloads_and_extracts_once_for_summary_and_quiz(tmp_path):
    review, canvas, ai, notifier, _selected, ended_at = runner(tmp_path)
    from academic_assistant.lecture_content import build_lecture_content_bundle as real_build

    with patch(
        "academic_assistant.lecture_quiz.build_lecture_content_bundle", wraps=real_build
    ) as build:
        report = review.process(
            now=ended_at + timedelta(minutes=15),
            include_summaries=True,
            include_quizzes=True,
        )

    assert report.summaries_sent == report.quizzes_sent == 1
    assert canvas.download_lecture_materials.call_count == 1
    assert build.call_count == 1
    assert ai.summary_calls == ai.quiz_calls == 1
    assert {destination for _, destination in notifier.calls} == {
        "lecture_summaries",
        "lecture_quizzes",
    }


def test_transient_summary_failure_does_not_block_quiz_delivery(tmp_path):
    review, _canvas, ai, notifier, _selected, ended_at = runner(tmp_path)
    ai.generate_lecture_summary = Mock(
        side_effect=AIProviderError("Gemini quota is temporarily exhausted.", transient=True)
    )

    report = review.process(
        now=ended_at + timedelta(minutes=15),
        include_summaries=True,
        include_quizzes=True,
    )

    assert report.summaries_failed == 1
    assert report.quizzes_sent == 1
    assert ("lecture-quiz:2026-09-14:csc:1400", "lecture_quizzes") in notifier.calls


def test_failed_delivery_reuses_cached_generation_without_download_or_gemini(tmp_path):
    AI_USAGE.reset()
    review, canvas, ai, _notifier, _selected, ended_at = runner(tmp_path, fail_summary_once=True)
    now = ended_at + timedelta(minutes=15)

    first = review.process(now=now, include_summaries=True, include_quizzes=False)
    second = review.process(now=now, include_summaries=True, include_quizzes=False)
    third = review.process(now=now, include_summaries=True, include_quizzes=False)

    assert first.summaries_failed == 1
    assert second.summaries_sent == 1
    assert third.summaries_already_sent == 1
    assert ai.summary_calls == 1
    assert canvas.download_lecture_materials.call_count == 1
    usage = AI_USAGE.snapshot()["lecture_summary"]
    assert usage.cache_misses == 1
    assert usage.cache_hits == 1
    record = review.store.cache_get("lecture-summary-session:v1:2026-09-14:csc:1400")
    assert record["prompt_version"] == 2
    assert record["model"] == "gemini-test"
    assert record["generated_at"].endswith("+00:00")
    assert record["sources"][0]["content_hash"]


def test_yesterday_lecture_retries_when_material_appears_late(tmp_path):
    review, canvas, ai, _notifier, selected, _ended_at = runner(tmp_path)
    canvas.download_lecture_materials.side_effect = [
        SimpleNamespace(materials=(), warnings=()),
        SimpleNamespace(materials=(selected,), warnings=()),
    ]
    review._select_materials.side_effect = [(), (selected,)]

    now = datetime(2026, 9, 14, 14, 30, tzinfo=UTC)
    first = review.process(now=now, include_summaries=True, include_quizzes=False)
    second = review.process(now=now, include_summaries=True, include_quizzes=False)

    assert first.summaries_waiting_for_slides == 1
    assert first.summaries_sent == 0
    assert second.summaries_sent == 1
    assert ai.summary_calls == 1


def test_summary_generation_limit_defers_uncached_sessions_without_using_gemini(tmp_path):
    review, _canvas, ai, notifier, _selected, ended_at = runner(
        tmp_path, summary_generation_limit=1
    )
    earlier = ended_at - timedelta(days=1)
    review.schedule.ended_lecture_sessions.return_value = [
        (session(), earlier),
        (session(), ended_at),
    ]
    review.max_age_minutes = 10_000

    report = review.process(
        now=ended_at + timedelta(minutes=15),
        include_summaries=True,
        include_quizzes=False,
    )

    assert report.summaries_sent == 1
    assert report.summaries_deferred == 1
    assert ai.summary_calls == 1
    assert len(notifier.calls) == 1
    assert any("quota-safe generation limit reached" in item for item in report.warnings)


def test_stale_lecture_is_not_downloaded_generated_or_delivered(tmp_path):
    review, canvas, ai, notifier, _selected, ended_at = runner(tmp_path)

    report = review.process(
        now=ended_at + timedelta(minutes=61),
        include_summaries=True,
        include_quizzes=True,
    )

    assert report.summaries_sent == report.quizzes_sent == 0
    canvas.download_lecture_materials.assert_not_called()
    assert ai.summary_calls == ai.quiz_calls == 0
    assert notifier.calls == []


def test_future_or_not_yet_ended_lecture_does_not_download_or_generate(tmp_path):
    review, canvas, ai, _notifier, _selected, _ended_at = runner(tmp_path)
    review.schedule.ended_lecture_sessions.return_value = []

    report = review.process(include_summaries=True, include_quizzes=True)

    assert report.summaries_sent == report.quizzes_sent == 0
    canvas.download_lecture_materials.assert_not_called()
    assert ai.summary_calls == ai.quiz_calls == 0

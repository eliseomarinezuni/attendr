from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from google.genai import errors

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from academic_assistant import (
    AIAssistant,
    AIConfigurationError,
    AIInputError,
    AIProviderError,
    extract_pdf_text_chunks,
    quiz_discord_payload,
    send_quiz_to_discord,
)


def question(number: int) -> dict:
    return {
        "question": f"Conceptual question {number}?",
        "options": [
            f"Option A{number}",
            f"Option B{number}",
            f"Option C{number}",
            f"Option D{number}",
        ],
        "correct_answer": "B",
        "explanation": f"B is correct for conceptual reason {number}.",
    }


class FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=self.responses.pop(0))


class FakeInteractions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.responses.pop(0))


class FakeClient:
    def __init__(self, responses):
        self.models = FakeModels(responses)
        self.interactions = FakeInteractions(responses)


class FakeNotifier:
    def __init__(self):
        self.calls = []

    def send_custom_notification(self, event_key, payload, *, force=False):
        self.calls.append((event_key, payload, force))
        return True


class AIAssistantTests(unittest.TestCase):
    def test_generate_quiz_uses_structured_schema_and_returns_exact_count(self):
        fake = FakeClient([json.dumps([question(1), question(2), question(3)])])
        assistant = AIAssistant("test-key", client=fake)

        result = assistant.generate_quiz("Binary Search Trees", 3)

        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["correct_answer"], "B")
        call = fake.models.calls[0]
        self.assertEqual(call["model"], "gemini-3.6-flash")
        self.assertEqual(call["config"].response_mime_type, "application/json")
        self.assertIsNotNone(call["config"].response_schema)
        self.assertNotIn("test-key", call["contents"])

    def test_aq_key_uses_interactions_structured_output(self):
        fake = FakeClient(
            [json.dumps({"results": [question(1), question(2), question(3)]})]
        )
        assistant = AIAssistant("AQ.test-key", client=fake)

        result = assistant.generate_quiz("Binary Search Trees", 3)

        self.assertEqual(len(result), 3)
        self.assertEqual(fake.models.calls, [])
        call = fake.interactions.calls[0]
        self.assertNotIn("response_mime_type", call)
        self.assertEqual(call["response_format"]["type"], "text")
        self.assertIn("schema", call["response_format"])
        schema = call["response_format"]["schema"]
        self.assertNotIn("$defs", json.dumps(schema))
        self.assertNotIn("$ref", json.dumps(schema))

    def test_aq_key_accepts_text_from_interaction_outputs(self):
        fake = FakeClient([])
        fake.interactions.create = lambda **kwargs: SimpleNamespace(
            output_text=None,
            outputs=[
                SimpleNamespace(
                    text=json.dumps(
                        {"results": [question(1), question(2), question(3)]}
                    )
                )
            ],
        )
        assistant = AIAssistant("AQ.test-key", client=fake)

        result = assistant.generate_quiz("Binary Search Trees", 3)

        self.assertEqual(len(result), 3)

    def test_generate_quiz_rejects_wrong_count_and_invalid_options(self):
        fake = FakeClient([json.dumps([question(1), question(2)])])
        assistant = AIAssistant("test-key", client=fake)

        with self.assertRaisesRegex(AIProviderError, "expected 3"):
            assistant.generate_quiz("Photosynthesis", 3)

        with self.assertRaisesRegex(AIInputError, "3, 4, or 5"):
            assistant.generate_quiz("Photosynthesis", 2)

    def test_generated_objects_with_unexpected_keys_are_rejected(self):
        invalid = [question(1), question(2), question(3)]
        invalid[0]["unexpected"] = "must not pass validation"
        assistant = AIAssistant("test-key", client=FakeClient([json.dumps(invalid)]))

        with self.assertRaisesRegex(AIProviderError, "invalid structured data"):
            assistant.generate_quiz("Binary Search Trees", 3)

    def test_extract_syllabus_schedule_returns_clean_json_shape(self):
        response = [
            {
                "week_or_date": "Week 1",
                "topic": "Introduction",
                "readings_or_tasks": ["Chapter 1"],
                "major_deadlines": [],
            }
        ]
        fake = FakeClient([f"```json\n{json.dumps(response)}\n```"])
        assistant = AIAssistant("test-key", client=fake)

        result = assistant.extract_syllabus_schedule("Week 1: Introduction")

        self.assertEqual(result, response)

    def test_extract_major_deadlines_validates_grounded_iso_dates(self):
        response = [
            {
                "title": "Midterm Exam",
                "due_date": "2026-10-20",
                "due_time": "13:30",
                "kind": "exam",
                "source_evidence": "Midterm Exam: October 20 at 1:30 PM",
            }
        ]
        assistant = AIAssistant("test-key", client=FakeClient([json.dumps(response)]))

        result = assistant.extract_major_deadlines(
            "Midterm Exam: October 20, 2026 at 1:30 PM",
            course_name="Algorithms",
            source_title="Course syllabus",
        )

        self.assertEqual(result, response)

    def test_extract_major_deadlines_rejects_impossible_dates(self):
        response = [
            {
                "title": "Final Exam",
                "due_date": "2026-02-30",
                "due_time": None,
                "kind": "exam",
                "source_evidence": "Final Exam: February 30",
            }
        ]
        assistant = AIAssistant("test-key", client=FakeClient([json.dumps(response)]))

        with self.assertRaisesRegex(AIProviderError, "invalid structured data"):
            assistant.extract_major_deadlines(
                "Final Exam: February 30, 2026",
                course_name="Algorithms",
                source_title="Course syllabus",
            )

    def test_missing_key_and_oversized_material_fail_before_api_call(self):
        with self.assertRaisesRegex(AIConfigurationError, "GEMINI_API_KEY"):
            AIAssistant("")

        fake = FakeClient([])
        assistant = AIAssistant("test-key", max_input_chars=1_000, client=fake)
        with self.assertRaisesRegex(AIInputError, "safe limit"):
            assistant.generate_quiz("x" * 1_001)
        self.assertEqual(fake.models.calls, [])

    def test_provider_errors_are_classified_without_raw_details(self):
        provider_error = errors.ClientError(
            400,
            {
                "error": {
                    "message": "API key not valid: secret-value",
                    "status": "INVALID_ARGUMENT",
                }
            },
        )

        safe_error = AIAssistant._safe_provider_error(provider_error, "quiz generation")

        self.assertIn("rejected GEMINI_API_KEY", str(safe_error))
        self.assertNotIn("secret-value", str(safe_error))

    def test_pdf_extraction_cleans_and_chunks_consecutive_pages(self):
        pages = [
            SimpleNamespace(extract_text=lambda: "Binary search\n\ntrees " + "a" * 600),
            SimpleNamespace(extract_text=lambda: "Balancing\n\nrotations " + "b" * 600),
        ]
        fake_reader = SimpleNamespace(is_encrypted=False, pages=pages)
        with (
            tempfile.NamedTemporaryFile(suffix=".pdf") as pdf_file,
            patch(
                "academic_assistant.ai_assistant.PdfReader", return_value=fake_reader
            ),
        ):
            chunks = extract_pdf_text_chunks(
                pdf_file.name, max_chars=1_000, overlap_chars=100
            )

        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].page_start, 1)
        self.assertEqual(chunks[1].page_end, 2)
        self.assertIn("Binary search", chunks[0].text)

    def test_discord_payload_hides_answers_and_dispatches_through_notifier(self):
        questions = [question(1), question(2), question(3)]
        questions[0]["explanation"] = "Never expose ||nested spoiler|| markers."

        payload = quiz_discord_payload("Binary Search Trees", questions)

        self.assertEqual(len(payload["embeds"]), 3)
        answer = payload["embeds"][0]["fields"][1]["value"]
        self.assertTrue(answer.startswith("||"))
        self.assertTrue(answer.endswith("||"))
        self.assertIn("Answer: B", answer)
        self.assertIn("｜｜nested spoiler｜｜", answer)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

        notifier = FakeNotifier()
        sent = send_quiz_to_discord(
            notifier,
            "Binary Search Trees",
            questions,
            force=True,
            event_key="test-quiz",
        )
        self.assertTrue(sent)
        self.assertEqual(notifier.calls[0][0], "test-quiz")
        self.assertTrue(notifier.calls[0][2])


if __name__ == "__main__":
    unittest.main()

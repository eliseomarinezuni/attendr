"""Compatibility imports for Module 4 course-material and quiz tools."""

from .ai_assistant import (
    AIAssistant,
    AIConfigurationError,
    AIInputError,
    AIProviderError,
    PDFExtractionError,
    PDFTextChunk,
    QuizQuestion,
    SyllabusEntry,
    extract_pdf_text_chunks,
    quiz_discord_payload,
    send_quiz_to_discord,
)

__all__ = [
    "AIAssistant",
    "AIConfigurationError",
    "AIInputError",
    "AIProviderError",
    "PDFExtractionError",
    "PDFTextChunk",
    "QuizQuestion",
    "SyllabusEntry",
    "extract_pdf_text_chunks",
    "quiz_discord_payload",
    "send_quiz_to_discord",
]

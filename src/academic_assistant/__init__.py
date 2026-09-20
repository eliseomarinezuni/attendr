"""Public integration exports, loaded only when requested."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ai_assistant import AIAssistant as AIAssistant
    from .ai_assistant import AIConfigurationError as AIConfigurationError
    from .ai_assistant import AIInputError as AIInputError
    from .ai_assistant import AIProviderError as AIProviderError
    from .ai_assistant import MajorDeadline as MajorDeadline
    from .ai_assistant import HybridQuizQuestion as HybridQuizQuestion
    from .ai_assistant import GroundedSummaryPoint as GroundedSummaryPoint
    from .ai_assistant import LectureSummary as LectureSummary
    from .ai_assistant import PDFExtractionError as PDFExtractionError
    from .ai_assistant import PDFTextChunk as PDFTextChunk
    from .ai_assistant import QuizQuestion as QuizQuestion
    from .ai_assistant import SyllabusEntry as SyllabusEntry
    from .ai_assistant import extract_pdf_text_chunks as extract_pdf_text_chunks
    from .ai_assistant import extract_powerpoint_text_chunks as extract_powerpoint_text_chunks
    from .ai_assistant import hybrid_quiz_discord_payload as hybrid_quiz_discord_payload
    from .ai_assistant import quiz_discord_payload as quiz_discord_payload
    from .ai_assistant import send_quiz_to_discord as send_quiz_to_discord
    from .ai_assistant import send_hybrid_quiz_to_discord as send_hybrid_quiz_to_discord
    from .calendar_sync import CalendarAPIError as CalendarAPIError
    from .calendar_sync import CalendarAuthenticationError as CalendarAuthenticationError
    from .calendar_sync import CalendarConfigurationError as CalendarConfigurationError
    from .calendar_sync import CalendarSyncEntry as CalendarSyncEntry
    from .calendar_sync import CalendarSyncReport as CalendarSyncReport
    from .calendar_sync import GoogleCalendarAuthenticator as GoogleCalendarAuthenticator
    from .calendar_sync import GoogleCalendarSync as GoogleCalendarSync
    from .canvas_client import AcademicItem as AcademicItem
    from .canvas_client import Announcement as Announcement
    from .canvas_client import CanvasAPIError as CanvasAPIError
    from .canvas_client import CanvasAuthenticationError as CanvasAuthenticationError
    from .canvas_client import CanvasClient as CanvasClient
    from .canvas_client import CanvasConfigurationError as CanvasConfigurationError
    from .canvas_client import CanvasSnapshot as CanvasSnapshot
    from .canvas_client import CourseSummary as CourseSummary
    from .canvas_client import MaterialDownloadReport as MaterialDownloadReport
    from .canvas_client import LectureMaterial as LectureMaterial
    from .canvas_client import LectureMaterialDownloadReport as LectureMaterialDownloadReport
    from .canvas_client import SyllabusMaterial as SyllabusMaterial
    from .announcement_dates import AnnouncementDatesReport as AnnouncementDatesReport
    from .announcement_dates import AnnouncementDatesSync as AnnouncementDatesSync
    from .course_schedule import AcademicDate as AcademicDate
    from .course_schedule import ClassSession as ClassSession
    from .course_schedule import CourseSchedule as CourseSchedule
    from .lecture_quiz import LectureQuizReport as LectureQuizReport
    from .lecture_quiz import LectureReviewReport as LectureReviewReport
    from .lecture_quiz import LectureQuizRunner as LectureQuizRunner
    from .lecture_content import LectureContentBundle as LectureContentBundle
    from .lecture_content import LectureContentSource as LectureContentSource
    from .lecture_content import build_lecture_content_bundle as build_lecture_content_bundle
    from .lecture_summary import lecture_summary_discord_payload as lecture_summary_discord_payload
    from .materials_sync import CourseMaterialsSync as CourseMaterialsSync
    from .materials_sync import MaterialsConfigurationError as MaterialsConfigurationError
    from .materials_sync import MaterialsStateError as MaterialsStateError
    from .materials_sync import MaterialsSyncReport as MaterialsSyncReport
    from .notifier import DiscordConfigurationError as DiscordConfigurationError
    from .notifier import DiscordNotificationError as DiscordNotificationError
    from .notifier import DiscordQuietHoursError as DiscordQuietHoursError
    from .notifier import DiscordNotifier as DiscordNotifier
    from .notifier import DiscordRateLimitError as DiscordRateLimitError
    from .notifier import JsonNotificationState as JsonNotificationState
    from .notifier import NotificationReport as NotificationReport
    from .notifier import NotificationState as NotificationState
    from .study_planner import BusyInterval as BusyInterval
    from .study_planner import StudyPlanReport as StudyPlanReport
    from .study_planner import StudyPlanner as StudyPlanner
    from .study_planner import StudyRemoteState as StudyRemoteState
    from .study_planner import StudyTemplate as StudyTemplate


_EXPORTS = {
    "AIAssistant": "ai_assistant",
    "AIConfigurationError": "ai_assistant",
    "AIInputError": "ai_assistant",
    "AIProviderError": "ai_assistant",
    "MajorDeadline": "ai_assistant",
    "HybridQuizQuestion": "ai_assistant",
    "GroundedSummaryPoint": "ai_assistant",
    "LectureSummary": "ai_assistant",
    "PDFExtractionError": "ai_assistant",
    "PDFTextChunk": "ai_assistant",
    "QuizQuestion": "ai_assistant",
    "SyllabusEntry": "ai_assistant",
    "extract_pdf_text_chunks": "ai_assistant",
    "extract_powerpoint_text_chunks": "ai_assistant",
    "hybrid_quiz_discord_payload": "ai_assistant",
    "quiz_discord_payload": "ai_assistant",
    "send_quiz_to_discord": "ai_assistant",
    "send_hybrid_quiz_to_discord": "ai_assistant",
    "CalendarAPIError": "calendar_sync",
    "CalendarAuthenticationError": "calendar_sync",
    "CalendarConfigurationError": "calendar_sync",
    "CalendarSyncEntry": "calendar_sync",
    "CalendarSyncReport": "calendar_sync",
    "GoogleCalendarAuthenticator": "calendar_sync",
    "GoogleCalendarSync": "calendar_sync",
    "AcademicItem": "canvas_client",
    "Announcement": "canvas_client",
    "CanvasAPIError": "canvas_client",
    "CanvasAuthenticationError": "canvas_client",
    "CanvasClient": "canvas_client",
    "CanvasConfigurationError": "canvas_client",
    "CanvasSnapshot": "canvas_client",
    "CourseSummary": "canvas_client",
    "MaterialDownloadReport": "canvas_client",
    "LectureMaterial": "canvas_client",
    "LectureMaterialDownloadReport": "canvas_client",
    "SyllabusMaterial": "canvas_client",
    "AnnouncementDatesReport": "announcement_dates",
    "AnnouncementDatesSync": "announcement_dates",
    "AcademicDate": "course_schedule",
    "ClassSession": "course_schedule",
    "CourseSchedule": "course_schedule",
    "LectureQuizReport": "lecture_quiz",
    "LectureReviewReport": "lecture_quiz",
    "LectureQuizRunner": "lecture_quiz",
    "LectureContentBundle": "lecture_content",
    "LectureContentSource": "lecture_content",
    "build_lecture_content_bundle": "lecture_content",
    "lecture_summary_discord_payload": "lecture_summary",
    "CourseMaterialsSync": "materials_sync",
    "MaterialsConfigurationError": "materials_sync",
    "MaterialsStateError": "materials_sync",
    "MaterialsSyncReport": "materials_sync",
    "DiscordConfigurationError": "notifier",
    "DiscordNotificationError": "notifier",
    "DiscordQuietHoursError": "notifier",
    "DiscordNotifier": "notifier",
    "DiscordRateLimitError": "notifier",
    "JsonNotificationState": "notifier",
    "NotificationReport": "notifier",
    "NotificationState": "notifier",
    "BusyInterval": "study_planner",
    "StudyPlanReport": "study_planner",
    "StudyPlanner": "study_planner",
    "StudyRemoteState": "study_planner",
    "StudyTemplate": "study_planner",
}
__all__ = [
    "AIAssistant",
    "AIConfigurationError",
    "AIInputError",
    "AIProviderError",
    "MajorDeadline",
    "HybridQuizQuestion",
    "GroundedSummaryPoint",
    "LectureSummary",
    "PDFExtractionError",
    "PDFTextChunk",
    "QuizQuestion",
    "SyllabusEntry",
    "extract_pdf_text_chunks",
    "extract_powerpoint_text_chunks",
    "hybrid_quiz_discord_payload",
    "quiz_discord_payload",
    "send_quiz_to_discord",
    "send_hybrid_quiz_to_discord",
    "CalendarAPIError",
    "CalendarAuthenticationError",
    "CalendarConfigurationError",
    "CalendarSyncEntry",
    "CalendarSyncReport",
    "GoogleCalendarAuthenticator",
    "GoogleCalendarSync",
    "AcademicItem",
    "Announcement",
    "CanvasAPIError",
    "CanvasAuthenticationError",
    "CanvasClient",
    "CanvasConfigurationError",
    "CanvasSnapshot",
    "CourseSummary",
    "MaterialDownloadReport",
    "LectureMaterial",
    "LectureMaterialDownloadReport",
    "SyllabusMaterial",
    "AnnouncementDatesReport",
    "AnnouncementDatesSync",
    "AcademicDate",
    "ClassSession",
    "CourseSchedule",
    "LectureQuizReport",
    "LectureReviewReport",
    "LectureQuizRunner",
    "LectureContentBundle",
    "LectureContentSource",
    "build_lecture_content_bundle",
    "lecture_summary_discord_payload",
    "CourseMaterialsSync",
    "MaterialsConfigurationError",
    "MaterialsStateError",
    "MaterialsSyncReport",
    "DiscordConfigurationError",
    "DiscordNotificationError",
    "DiscordQuietHoursError",
    "DiscordNotifier",
    "DiscordRateLimitError",
    "JsonNotificationState",
    "NotificationReport",
    "NotificationState",
    "BusyInterval",
    "StudyPlanReport",
    "StudyPlanner",
    "StudyRemoteState",
    "StudyTemplate",
]


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(f".{_EXPORTS[name]}", __name__), name)
    globals()[name] = value
    return value

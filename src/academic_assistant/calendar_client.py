"""Compatibility imports for the Module 3 Google Calendar integration."""

from .calendar_sync import (
    CalendarAPIError,
    CalendarAuthenticationError,
    CalendarConfigurationError,
    CalendarSyncEntry,
    CalendarSyncReport,
    GoogleCalendarAuthenticator,
    GoogleCalendarSync,
)

__all__ = [
    "CalendarAPIError",
    "CalendarAuthenticationError",
    "CalendarConfigurationError",
    "CalendarSyncEntry",
    "CalendarSyncReport",
    "GoogleCalendarAuthenticator",
    "GoogleCalendarSync",
]

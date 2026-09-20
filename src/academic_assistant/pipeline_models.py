"""Shared pipeline selection and result types."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunPlan:
    announcements: bool
    materials: bool
    calendar: bool
    digest: bool
    quiz: bool
    lecture_quizzes: bool
    study_plan: bool
    review: bool = False
    lecture_summaries: bool = False

    @property
    def needs_canvas(self) -> bool:
        return (
            self.announcements
            or self.materials
            or self.calendar
            or self.digest
            or self.lecture_quizzes
            or self.lecture_summaries
            or self.study_plan
        )


@dataclass(frozen=True, slots=True)
class StepResult:
    name: str
    status: str
    detail: str

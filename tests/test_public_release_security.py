from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PRODUCTION_WORKFLOWS = ("schedule.yml", "class-quizzes.yml")
PRIVATE_DATA_FILES = (
    "announcement_dates_index.json",
    "course_schedule.json",
    "daily_topics.json",
    "lecture_quiz_state.json",
    "materials_index.json",
    "preferences.json",
    "seen_ids.json",
)


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_pull_request_ci_never_receives_owner_secrets() -> None:
    workflow = _workflow("assistant.yml")

    assert "pull_request:" in workflow
    assert "pull_request_target:" not in workflow
    assert "secrets.CANVAS" not in workflow
    assert "secrets.GEMINI" not in workflow
    assert "secrets.DISCORD" not in workflow
    assert "secrets.GOOGLE" not in workflow
    assert "secrets.STUDY_SYNC_SECRET" not in workflow
    assert "secrets.ATTENDR_STATE_KEY" not in workflow


def test_production_workflows_are_limited_to_owner_main() -> None:
    guard = "github.repository == 'eliseomarinezuni/attendr' && github.ref == 'refs/heads/main'"

    for name in PRODUCTION_WORKFLOWS:
        workflow = _workflow(name)
        assert "pull_request:" not in workflow
        assert "pull_request_target:" not in workflow
        assert guard in workflow
        assert "secrets.COURSE_SCHEDULE_B64" in workflow
        assert "secrets.ATTENDR_PREFERENCES_B64" in workflow


def test_checkout_credentials_are_not_persisted() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        workflow = path.read_text(encoding="utf-8")
        checkout_count = workflow.count("uses: actions/checkout@")
        assert workflow.count("persist-credentials: false") == checkout_count


def test_private_runtime_data_is_ignored_and_examples_are_tracked() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    for name in PRIVATE_DATA_FILES:
        assert f"data/{name}" in ignore

    assert (ROOT / "data" / "course_schedule.example.json").is_file()
    assert (ROOT / "data" / "preferences.example.json").is_file()


def test_worker_google_credentials_have_a_safe_scoped_refresh_command() -> None:
    script = (ROOT / "scripts" / "configure_cloudflare_secrets.py").read_text(encoding="utf-8")

    assert '"--google-only"' in script
    assert "/api/google/health" in script
    assert "response.text" not in script
    assert "response.content" not in script

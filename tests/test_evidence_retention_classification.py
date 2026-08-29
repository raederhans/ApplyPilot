from __future__ import annotations

import json
from pathlib import Path

from applypilot.apply import launcher
from applypilot.apply.retention import OWNERSHIP_MARKER


def _configure(monkeypatch, tmp_path: Path) -> Path:
    log_root = tmp_path / "logs"
    monkeypatch.setattr(launcher.config, "LOG_DIR", log_root)
    monkeypatch.setattr(launcher, "_evidence_retention_checked", True)
    return log_root


def _marker(destination: Path) -> dict[str, object]:
    return json.loads((destination / OWNERSHIP_MARKER).read_text(encoding="utf-8"))


def test_neutral_observer_names_are_archived_under_their_real_names(
    monkeypatch, tmp_path: Path
) -> None:
    log_root = _configure(monkeypatch, tmp_path)
    worker = tmp_path / "worker"
    worker.mkdir()
    (worker / "post-submit-observer.png").write_bytes(b"neutral observation")
    archived = launcher._archive_worker_evidence(
        worker,
        {"url": "https://example.test/job", "_attempt_id": "attempt-1"},
        0,
        "20260829_200000",
        disposition="historical_duplicate",
    )
    assert [path.name for path in archived] == ["post-submit-observer.png"]
    manifest = json.loads(
        (archived[0].parent / "evidence-manifest.json").read_text(encoding="utf-8")
    )
    assert set(manifest) == {"post-submit-observer.png"}
    assert archived[0].is_relative_to(log_root / "application-evidence")


def test_filename_never_promotes_retention_state_to_applied(
    monkeypatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    destination = launcher.config.LOG_DIR / "application-evidence" / "attempt"
    destination.mkdir(parents=True)
    screenshot = destination / "submission-confirmation.png"
    screenshot.write_bytes(b"ambiguous screenshot")
    launcher._record_evidence_retention(
        destination,
        [screenshot],
        {"_attempt_id": "attempt-1"},
        disposition="uncertain",
        receipt_admitted=False,
    )
    marker = _marker(destination)
    assert marker["kind"] == "job_transient"
    assert marker["state"] == "submission_uncertain"


def test_conflicting_post_submit_status_is_retained_as_uncertain(
    monkeypatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    destination = launcher.config.LOG_DIR / "application-evidence" / "conflict"
    destination.mkdir(parents=True)
    observer = destination / "post-submit-observer.png"
    observer.write_bytes(b"conflicting page signals")
    launcher._record_evidence_retention(
        destination,
        [observer],
        {"_attempt_id": "attempt-conflict"},
        disposition="conflicting_post_submit_status",
    )
    assert _marker(destination)["state"] == "submission_uncertain"


def test_only_explicit_durable_receipt_admission_marks_applied(
    monkeypatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    destination = launcher.config.LOG_DIR / "application-evidence" / "attempt"
    destination.mkdir(parents=True)
    observer = destination / "post-submit-observer.png"
    observer.write_bytes(b"receipt-observer")
    launcher._record_evidence_retention(
        destination,
        [observer],
        {"_attempt_id": "attempt-1"},
        disposition="confirmed",
        receipt_admitted=True,
    )
    marker = _marker(destination)
    assert marker["kind"] == "application_evidence"
    assert marker["state"] == "applied"


def test_historical_duplicate_is_persistent_but_not_new_application(
    monkeypatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    destination = launcher.config.LOG_DIR / "application-evidence" / "attempt"
    destination.mkdir(parents=True)
    observer = destination / "post-submit-observer.png"
    observer.write_bytes(b"already applied")
    launcher._record_evidence_retention(
        destination,
        [observer],
        {"_attempt_id": "attempt-1"},
        disposition="historical_duplicate",
    )
    marker = _marker(destination)
    assert marker["kind"] == "application_evidence"
    assert marker["state"] == "historical_duplicate"

from __future__ import annotations

from applypilot.apply.browser_broker import BrowserLease, BrowserLeaseBundle
from applypilot.apply.page_binding import PageBinding
from applypilot.apply.page_observation import _verified_agent_resume_upload
from applypilot.apply.prepared_state import (
    bind_prepared_state_evidence,
    current_prepared_observations,
)


def _binding(*, page_epoch: int = 1, lease_id: str = "page-lease") -> dict[str, object]:
    profile = BrowserLease(
        lease_id="profile-lease",
        resource_kind="profile",
        resource_id="profile-0",
        owner_id="application:attempt-1",
        scope_id="worker:0",
        attempt_id="attempt-1",
        runtime_id="edge:1234",
        epoch=1,
        issued_at=1.0,
        heartbeat_at=2.0,
        expires_at=30.0,
    )
    page = BrowserLease(
        lease_id=lease_id,
        resource_kind="page",
        resource_id="application:attempt-1",
        owner_id="application:attempt-1",
        scope_id="worker:0",
        attempt_id="attempt-1",
        runtime_id="edge:1234",
        epoch=1,
        issued_at=1.0,
        heartbeat_at=2.0,
        expires_at=30.0,
    )
    binding = PageBinding(
        page_id=page.resource_id,
        page_lease_id=page.lease_id,
        page_lease_epoch=page.epoch,
        page_epoch=page_epoch,
        profile_lease_id=profile.lease_id,
        owner_id=page.owner_id,
        attempt_id=page.attempt_id,
        runtime_id=page.runtime_id,
    )
    return BrowserLeaseBundle(profile, page, binding).as_dict()


def test_prepared_upload_proof_survives_a_same_page_repair_turn() -> None:
    job = {
        "_attempt_id": "attempt-1",
        "_browser_lease_binding": _binding(page_epoch=3),
    }

    bind_prepared_state_evidence(
        job,
        run_id="prepare-1",
        observations={
            "resume_upload": {
                "verified": True,
                "field_label": "Resume/CV",
                "visible_filename": True,
            }
        },
    )
    job["_browser_lease_binding"] = _binding(page_epoch=4)
    bind_prepared_state_evidence(job, run_id="repair-2", observations={})

    assert current_prepared_observations(job)["resume_upload"]["verified"] is True


def test_prepared_upload_proof_does_not_cross_a_new_page_lease() -> None:
    job = {
        "_attempt_id": "attempt-1",
        "_browser_lease_binding": _binding(page_epoch=3),
    }
    bind_prepared_state_evidence(
        job,
        run_id="prepare-1",
        observations={
            "resume_upload": {
                "verified": True,
                "field_label": "Resume/CV",
                "visible_filename": True,
            }
        },
    )

    job["_browser_lease_binding"] = _binding(page_epoch=0, lease_id="replacement-page-lease")

    assert current_prepared_observations(job) == {}
    assert _verified_agent_resume_upload(job) is False


def test_prepared_state_keeps_only_bounded_corroboration_proofs() -> None:
    job = {
        "_attempt_id": "attempt-1",
        "_browser_lease_binding": _binding(),
    }

    evidence = bind_prepared_state_evidence(
        job,
        run_id="prepare-1",
        observations={
            "resume_upload": {
                "verified": True,
                "field_label": "Resume/CV",
                "visible_filename": "candidate.pdf",
            },
            "free_form_answer": "must not be persisted",
        },
    )

    assert set(evidence["observations"]) == {"resume_upload"}
    assert evidence["observations"]["resume_upload"]["visible_filename"] is True
    assert _verified_agent_resume_upload(job) is True

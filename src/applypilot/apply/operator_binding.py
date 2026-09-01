"""Host-derived, reference-only bindings for resumable operator exceptions."""

from __future__ import annotations

from collections.abc import Mapping

from applypilot.apply.browser_broker import BrowserBrokerError, BrowserLeaseBundle
from applypilot.apply.contracts import DecisionEnvelope

OPERATOR_RESUME_BINDING_KEYS = frozenset(
    {
        "request_id",
        "checkpoint_id",
        "job_url",
        "profile_id",
        "browser_lease_id",
        "browser_lease_epoch",
        "page_target_id",
        "page_epoch",
    }
)


def operator_resume_binding(
    decision: DecisionEnvelope,
    job: Mapping[str, object],
) -> dict[str, object] | None:
    """Build a complete host-only resume binding or return no binding.

    The result contains durable identities and epochs only. It never carries a
    human answer, credential, page observation, provider session, or authority.
    """
    recovery = decision.recovery_action
    if recovery is None or recovery.action != "human_only":
        return None
    parent_turn_id = str(job.get("_parent_agent_run_id") or "").strip()
    checkpoint_id = str(job.get("_parent_agent_checkpoint_id") or "").strip()
    job_url = str(job.get("url") or "").strip()
    attempt_id = str(job.get("_attempt_id") or "").strip()
    raw_bundle = job.get("_browser_lease_binding")
    if (
        parent_turn_id != decision.run_id
        or attempt_id != decision.attempt_id
        or not checkpoint_id
        or not job_url
        or not isinstance(raw_bundle, Mapping)
    ):
        return None
    try:
        bundle = BrowserLeaseBundle.from_mapping(raw_bundle)
    except (BrowserBrokerError, TypeError, ValueError):
        return None
    if (
        bundle.profile.attempt_id != decision.attempt_id
        or bundle.page.attempt_id != decision.attempt_id
        or bundle.profile.owner_id != decision.actor_id
        or bundle.page.owner_id != decision.actor_id
        or bundle.profile.lease_id != bundle.page.lease_id
        or bundle.page_binding.page_lease_id != bundle.page.lease_id
        or bundle.page_binding.page_id != bundle.page.resource_id
    ):
        return None
    return {
        "request_id": f"{decision.run_id}:human:1",
        "checkpoint_id": checkpoint_id,
        "job_url": job_url,
        "profile_id": bundle.profile.resource_id,
        "browser_lease_id": bundle.profile.lease_id,
        "browser_lease_epoch": bundle.profile.epoch,
        "page_target_id": bundle.page.resource_id,
        "page_epoch": bundle.page_binding.page_epoch,
    }

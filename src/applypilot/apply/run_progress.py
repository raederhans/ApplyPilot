"""Thread-safe run-level progress and bounded work tickets.

This module contains no database or browser behavior.  It gives concurrent
workers one shared, deterministic view of preview capacity, real submission
capacity, and receipt-confirmed completion.
"""

from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PreviewTicket:
    """One temporary claim against the dry-run preview target."""

    token: str
    item_key: str


@dataclass(frozen=True, slots=True)
class SubmitDecision:
    """Result of the run-local check immediately before durable reservation."""

    allowed: bool
    reason: str
    replay: bool = False


class RunProgress:
    """Coordinate one application run without conflating targets and authority."""

    def __init__(
        self,
        *,
        dry_run: bool,
        success_target: int,
        preview_target: int,
        authorization_slot_cap: int,
        run_id: str | None = None,
    ) -> None:
        for name, value in (
            ("success_target", success_target),
            ("preview_target", preview_target),
            ("authorization_slot_cap", authorization_slot_cap),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.dry_run = bool(dry_run)
        self.run_id = str(run_id or f"apply-run-{uuid.uuid4()}").strip()
        if not self.run_id:
            raise ValueError("run_id is required")
        self.success_target = success_target
        self.preview_target = preview_target
        self.authorization_slot_cap = authorization_slot_cap
        self._lock = threading.RLock()
        self._preview_claims: dict[str, PreviewTicket] = {}
        self._preview_consumed: set[str] = set()
        self._authorization_claims: set[str] = set()
        self._terminal: dict[str, tuple[str, bool]] = {}
        self._manifest_exhausted = False
        self._performance_samples = 0
        self._performance_totals: dict[str, float] = {}
        self._performance_maxima: dict[str, float] = {}
        self._acquisition_attempts = 0
        self._acquisition_outcomes: dict[str, int] = {}
        self._acquisition_totals: dict[str, float] = {}
        self._acquisition_maxima: dict[str, float] = {}

    _PERFORMANCE_KEYS = frozenset(
        {
            "pre_submit_audit_ms",
            "submission_gate_wait_ms",
            "submit_agent_ms",
            "post_submit_observer_ms",
            "prepare_repair_agent_ms",
            "validation_repair_agent_ms",
            "submit_lane_wait_ms",
            "submit_lane_hold_ms",
            "submit_lane_acquisitions",
            "submit_lane_peak",
        }
    )
    _ACQUISITION_KEYS = frozenset(
        {
            "worker_call_ms",
            "stale_recovery_ms",
            "profile_load_ms",
            "eligibility_refresh_ms",
            "transaction_wait_ms",
            "candidate_fetch_ms",
            "candidate_rows",
            "admission_scan_ms",
            "admission_rows_scanned",
            "total_ms",
        }
    )
    _PERFORMANCE_VALUE_CAP = 86_400_000.0
    _PERFORMANCE_MAX_ONLY_KEYS = frozenset({"submit_lane_peak"})

    @staticmethod
    def _key(item_key: object) -> str:
        value = str(item_key).strip()
        if not value:
            raise ValueError("item_key is required")
        return value

    def should_acquire(self) -> bool:
        """Return whether another manifest item can still advance this run."""
        with self._lock:
            if self._manifest_exhausted:
                return False
            if self.dry_run:
                return (
                    len(self._preview_consumed) + len(self._preview_claims)
                    < self.preview_target
                )
            if self._receipt_confirmed_successes() >= self.success_target:
                return False
            return len(self._authorization_claims) < self.authorization_slot_cap

    def claim_preview_ticket(self, item_key: object) -> PreviewTicket | None:
        """Atomically reserve one dry-run preview slot, enforcing a strict cap."""
        key = self._key(item_key)
        with self._lock:
            if not self.dry_run or self._manifest_exhausted:
                return None
            if key in self._preview_consumed:
                return None
            for ticket in self._preview_claims.values():
                if ticket.item_key == key:
                    return ticket
            if len(self._preview_consumed) + len(self._preview_claims) >= self.preview_target:
                return None
            ticket = PreviewTicket(token=uuid.uuid4().hex, item_key=key)
            self._preview_claims[ticket.token] = ticket
            return ticket

    def release_preview_ticket(self, ticket: PreviewTicket) -> bool:
        """Release an unfinished preview ticket so another item may use it."""
        with self._lock:
            current = self._preview_claims.get(ticket.token)
            if current != ticket:
                return False
            del self._preview_claims[ticket.token]
            return True

    def consume_preview_ticket(self, ticket: PreviewTicket) -> bool:
        """Permanently count one completed preview; replay is a no-op."""
        with self._lock:
            current = self._preview_claims.get(ticket.token)
            if current != ticket:
                return False
            del self._preview_claims[ticket.token]
            if ticket.item_key in self._preview_consumed:
                return False
            self._preview_consumed.add(ticket.item_key)
            return True

    def before_submit(self, item_key: object) -> SubmitDecision:
        """Claim run-local real capacity before the durable database gate.

        A repeated call for the same item is idempotently allowed.  This is an
        optimization and coordination check; the SQLite gate remains the final
        cross-process authority.
        """
        key = self._key(item_key)
        with self._lock:
            if self.dry_run:
                return SubmitDecision(False, "dry_run_submission_forbidden")
            if key in self._authorization_claims:
                return SubmitDecision(True, "submission_slot_replay", replay=True)
            if self._receipt_confirmed_successes() >= self.success_target:
                return SubmitDecision(False, "run_success_target_reached")
            if len(self._authorization_claims) >= self.authorization_slot_cap:
                return SubmitDecision(False, "authorization_batch_capacity_exhausted")
            self._authorization_claims.add(key)
            return SubmitDecision(True, "submission_slot_claimed")

    def record_terminal(
        self,
        item_key: object,
        outcome: str,
        *,
        receipt_confirmed: bool = False,
    ) -> bool:
        """Record the first terminal outcome for an item.

        Only ``applied`` with an admitted receipt advances the real success
        target.  Uncertain outcomes never release an authorization reservation.
        """
        key = self._key(item_key)
        normalized = str(outcome or "unknown").strip().casefold() or "unknown"
        with self._lock:
            if key in self._terminal:
                return False
            confirmed = normalized == "applied" and bool(receipt_confirmed)
            self._terminal[key] = (normalized, confirmed)
            if normalized == "cancelled_before_action":
                self._authorization_claims.discard(key)
            return True

    def mark_manifest_exhausted(self) -> None:
        """Declare that the producer has no more eligible manifest items."""
        with self._lock:
            self._manifest_exhausted = True

    def record_performance(self, metrics: dict[str, object]) -> bool:
        """Aggregate one bounded worker sample without affecting run decisions."""
        normalized: dict[str, float] = {}
        for key, value in metrics.items():
            if key not in self._PERFORMANCE_KEYS:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            numeric = float(value)
            if not math.isfinite(numeric) or numeric < 0:
                continue
            normalized[key] = min(numeric, self._PERFORMANCE_VALUE_CAP)
        if not normalized:
            return False
        with self._lock:
            self._performance_samples += 1
            for key, value in normalized.items():
                if key in self._PERFORMANCE_MAX_ONLY_KEYS:
                    self._performance_totals[key] = max(
                        self._performance_totals.get(key, 0.0), value
                    )
                else:
                    self._performance_totals[key] = (
                        self._performance_totals.get(key, 0.0) + value
                    )
                self._performance_maxima[key] = max(
                    self._performance_maxima.get(key, 0.0), value
                )
        return True

    def record_acquisition(
        self,
        metrics: dict[str, object],
        *,
        outcome: str,
    ) -> bool:
        """Record every acquire attempt, including bounded empty outcomes."""
        normalized_outcome = str(outcome or "unknown").strip().casefold()
        if normalized_outcome not in {"acquired", "empty", "blocked", "error"}:
            normalized_outcome = "error"
        normalized: dict[str, float] = {}
        for key, value in metrics.items():
            if key not in self._ACQUISITION_KEYS:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            numeric = float(value)
            if not math.isfinite(numeric) or numeric < 0:
                continue
            normalized[key] = min(numeric, self._PERFORMANCE_VALUE_CAP)
        with self._lock:
            self._acquisition_attempts += 1
            self._acquisition_outcomes[normalized_outcome] = (
                self._acquisition_outcomes.get(normalized_outcome, 0) + 1
            )
            for key, value in normalized.items():
                self._acquisition_totals[key] = (
                    self._acquisition_totals.get(key, 0.0) + value
                )
                self._acquisition_maxima[key] = max(
                    self._acquisition_maxima.get(key, 0.0), value
                )
        return True

    def _receipt_confirmed_successes(self) -> int:
        return sum(confirmed for _outcome, confirmed in self._terminal.values())

    def snapshot(self) -> dict[str, object]:
        """Return a consistent, serialization-friendly progress snapshot."""
        with self._lock:
            successes = self._receipt_confirmed_successes()
            previews = len(self._preview_consumed)
            target_reached = (
                previews >= self.preview_target
                if self.dry_run
                else successes >= self.success_target
            )
            authorization_capacity_exhausted = (
                not self.dry_run
                and len(self._authorization_claims) >= self.authorization_slot_cap
            )
            outcome_counts: dict[str, int] = {}
            for outcome, _confirmed in self._terminal.values():
                outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
            return {
                "run_id": self.run_id,
                "dry_run": self.dry_run,
                "success_target": self.success_target,
                "preview_target": self.preview_target,
                "authorization_slot_cap": self.authorization_slot_cap,
                "receipt_confirmed_successes": successes,
                "previews_consumed": previews,
                "preview_tickets_claimed": len(self._preview_claims),
                "authorization_slots_used": len(self._authorization_claims),
                "terminal_items": len(self._terminal),
                "submission_uncertain": outcome_counts.get("submission_uncertain", 0),
                "outcomes": outcome_counts,
                "target_reached": target_reached,
                "authorization_capacity_exhausted": authorization_capacity_exhausted,
                "manifest_exhausted": self._manifest_exhausted,
                "performance": {
                    "job_sample_count": self._performance_samples,
                    "totals": {
                        key: round(value, 3)
                        for key, value in sorted(self._performance_totals.items())
                    },
                    "maxima": {
                        key: round(value, 3)
                        for key, value in sorted(self._performance_maxima.items())
                    },
                    "acquisition": {
                        "attempt_count": self._acquisition_attempts,
                        "outcomes": dict(sorted(self._acquisition_outcomes.items())),
                        "totals": {
                            key: round(value, 3)
                            for key, value in sorted(self._acquisition_totals.items())
                        },
                        "maxima": {
                            key: round(value, 3)
                            for key, value in sorted(self._acquisition_maxima.items())
                        },
                    },
                },
                "partial": (
                    self._manifest_exhausted or authorization_capacity_exhausted
                ) and not target_reached,
            }

"""Pure Workday application-page state contracts.

The observation surface is deliberately bounded: it records structural facts,
never field values, labels, URLs, browser handles, credentials, or receipts.
This module does not launch a browser or mutate the application ledger.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum

MAX_VISIBLE_CONTROLS = 128
MAX_VISIBLE_ERRORS = 64


class WorkdayPageKind(StrEnum):
    UNKNOWN = "unknown"
    SIGN_IN = "sign_in"
    RESUME_UPLOAD = "resume_upload"
    MY_INFORMATION = "my_information"
    MY_EXPERIENCE = "my_experience"
    APPLICATION_QUESTIONS = "application_questions"
    VOLUNTARY_DISCLOSURES = "voluntary_disclosures"
    SELF_IDENTIFICATION = "self_identification"
    REVIEW = "review"
    VALIDATION_ERROR = "validation_error"
    CONFIRMATION = "confirmation"
    MANUAL_GATE = "manual_gate"


class ControlKind(StrEnum):
    TEXT = "text"
    TEXTAREA = "textarea"
    SELECT = "select"
    COMBOBOX = "combobox"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    FILE_UPLOAD = "file_upload"
    DATE = "date"
    EMAIL = "email"
    PHONE = "phone"


class WorkdayState(StrEnum):
    START = "start"
    AUTHENTICATION = "authentication"
    UPLOAD = "upload"
    FORM = "form"
    REVIEW = "review"
    SUBMIT_STARTED = "submit_started"
    APPLIED = "applied"
    MANUAL_REVIEW = "manual_review"
    SUBMISSION_UNCERTAIN = "submission_uncertain"
    FAILED_STUCK = "failed_stuck"
    FAILED_VALIDATION = "failed_validation"


class ProgressAction(StrEnum):
    CONTINUE = "continue"
    REPAIR_ONCE = "repair_once"
    STOP_STUCK = "stop_stuck"
    STOP_MANUAL = "stop_manual"
    MARK_UNCERTAIN = "mark_uncertain"
    STOP_VALIDATION = "stop_validation"


@dataclass(frozen=True, slots=True)
class BoundedPageObservation:
    """Provider-neutral structural observation safe for control-plane storage."""

    page_kind: WorkdayPageKind
    step_index: int | None = None
    step_count: int | None = None
    visible_controls: tuple[ControlKind, ...] = ()
    required_count: int = 0
    invalid_count: int = 0
    has_next: bool = False
    has_review: bool = False
    has_submit: bool = False
    has_confirmation: bool = False
    has_manual_gate: bool = False
    repairable_validation: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.page_kind, WorkdayPageKind):
            raise TypeError("page_kind must be a WorkdayPageKind")
        if not isinstance(self.visible_controls, tuple):
            raise TypeError("visible_controls must be an immutable tuple")
        if len(self.visible_controls) > MAX_VISIBLE_CONTROLS:
            raise ValueError(f"visible_controls may contain at most {MAX_VISIBLE_CONTROLS} entries")
        if any(not isinstance(item, ControlKind) for item in self.visible_controls):
            raise TypeError("visible_controls must contain ControlKind values")
        for name in (
            "has_next",
            "has_review",
            "has_submit",
            "has_confirmation",
            "has_manual_gate",
            "repairable_validation",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")
        for name in ("required_count", "invalid_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_VISIBLE_ERRORS:
                raise ValueError(f"{name} must be an integer from 0 to {MAX_VISIBLE_ERRORS}")
        if (self.step_index is None) != (self.step_count is None):
            raise ValueError("step_index and step_count must be supplied together")
        if self.step_index is not None and (
            isinstance(self.step_index, bool)
            or isinstance(self.step_count, bool)
            or not isinstance(self.step_index, int)
            or not isinstance(self.step_count, int)
            or self.step_count < 1
            or not 1 <= self.step_index <= self.step_count <= 50
        ):
            raise ValueError("step position must satisfy 1 <= step_index <= step_count <= 50")
        if self.repairable_validation and self.invalid_count == 0:
            raise ValueError("repairable_validation requires at least one visible invalid control")

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["page_kind"] = self.page_kind.value
        result["visible_controls"] = [item.value for item in self.visible_controls]
        return result


_OBSERVATION_KEYS = {
    "page_kind",
    "step_index",
    "step_count",
    "visible_controls",
    "required_count",
    "invalid_count",
    "has_next",
    "has_review",
    "has_submit",
    "has_confirmation",
    "has_manual_gate",
    "repairable_validation",
}


def observation_from_mapping(value: Mapping[str, object]) -> BoundedPageObservation:
    """Parse only the fixed observation schema; reject raw or provider-owned data."""
    unexpected = sorted(set(value) - _OBSERVATION_KEYS)
    if unexpected:
        raise ValueError(f"unsupported observation fields: {', '.join(unexpected)}")
    controls = value.get("visible_controls") or ()
    if not isinstance(controls, (list, tuple)):
        raise TypeError("visible_controls must be an array")
    return BoundedPageObservation(
        page_kind=WorkdayPageKind(str(value.get("page_kind") or "unknown")),
        step_index=_optional_int(value.get("step_index"), "step_index"),
        step_count=_optional_int(value.get("step_count"), "step_count"),
        visible_controls=tuple(ControlKind(str(item)) for item in controls),
        required_count=_required_int(value.get("required_count", 0), "required_count"),
        invalid_count=_required_int(value.get("invalid_count", 0), "invalid_count"),
        has_next=_required_bool(value.get("has_next", False), "has_next"),
        has_review=_required_bool(value.get("has_review", False), "has_review"),
        has_submit=_required_bool(value.get("has_submit", False), "has_submit"),
        has_confirmation=_required_bool(value.get("has_confirmation", False), "has_confirmation"),
        has_manual_gate=_required_bool(value.get("has_manual_gate", False), "has_manual_gate"),
        repairable_validation=_required_bool(
            value.get("repairable_validation", False), "repairable_validation"
        ),
    )


def _optional_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _required_int(value, name)


def _required_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _required_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def page_signature(observation: BoundedPageObservation) -> str:
    """Return a deterministic signature derived only from bounded structure."""
    encoded = json.dumps(
        observation.as_dict(), sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()[:24]


_FORM_PAGE_KINDS = {
    WorkdayPageKind.MY_INFORMATION,
    WorkdayPageKind.MY_EXPERIENCE,
    WorkdayPageKind.APPLICATION_QUESTIONS,
    WorkdayPageKind.VOLUNTARY_DISCLOSURES,
    WorkdayPageKind.SELF_IDENTIFICATION,
    WorkdayPageKind.VALIDATION_ERROR,
}


def state_for_observation(observation: BoundedPageObservation) -> WorkdayState:
    """Classify one bounded snapshot without reading or writing business state."""
    if observation.has_confirmation or observation.page_kind is WorkdayPageKind.CONFIRMATION:
        return WorkdayState.APPLIED
    if observation.has_manual_gate or observation.page_kind is WorkdayPageKind.MANUAL_GATE:
        return WorkdayState.MANUAL_REVIEW
    if observation.page_kind is WorkdayPageKind.SIGN_IN:
        return WorkdayState.AUTHENTICATION
    if observation.page_kind is WorkdayPageKind.RESUME_UPLOAD:
        return WorkdayState.UPLOAD
    if observation.page_kind is WorkdayPageKind.REVIEW:
        return WorkdayState.REVIEW
    if observation.page_kind in _FORM_PAGE_KINDS:
        return WorkdayState.FORM
    return WorkdayState.START


_ALLOWED_TRANSITIONS: dict[WorkdayState, frozenset[WorkdayState]] = {
    WorkdayState.START: frozenset(
        {
            WorkdayState.AUTHENTICATION,
            WorkdayState.UPLOAD,
            WorkdayState.FORM,
            WorkdayState.REVIEW,
            WorkdayState.APPLIED,
            WorkdayState.MANUAL_REVIEW,
            WorkdayState.FAILED_STUCK,
        }
    ),
    WorkdayState.AUTHENTICATION: frozenset(
        {WorkdayState.UPLOAD, WorkdayState.FORM, WorkdayState.REVIEW, WorkdayState.MANUAL_REVIEW, WorkdayState.FAILED_STUCK}
    ),
    WorkdayState.UPLOAD: frozenset(
        {WorkdayState.FORM, WorkdayState.REVIEW, WorkdayState.MANUAL_REVIEW, WorkdayState.FAILED_STUCK}
    ),
    WorkdayState.FORM: frozenset(
        {WorkdayState.FORM, WorkdayState.REVIEW, WorkdayState.MANUAL_REVIEW, WorkdayState.FAILED_STUCK}
    ),
    WorkdayState.REVIEW: frozenset(
        {WorkdayState.FORM, WorkdayState.SUBMIT_STARTED, WorkdayState.MANUAL_REVIEW, WorkdayState.FAILED_STUCK}
    ),
    WorkdayState.SUBMIT_STARTED: frozenset(
        {
            WorkdayState.FORM,
            WorkdayState.APPLIED,
            WorkdayState.SUBMISSION_UNCERTAIN,
            WorkdayState.FAILED_VALIDATION,
        }
    ),
}


def transition_allowed(
    current: WorkdayState,
    target: WorkdayState,
    *,
    repair_authorized: bool = False,
) -> bool:
    """Guard legal state movement, including the one post-submit repair edge."""
    if target not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
        return False
    if current is WorkdayState.SUBMIT_STARTED and target is WorkdayState.FORM:
        return repair_authorized
    return True


def require_transition(
    current: WorkdayState,
    target: WorkdayState,
    *,
    repair_authorized: bool = False,
) -> WorkdayState:
    if not transition_allowed(current, target, repair_authorized=repair_authorized):
        raise ValueError(f"illegal Workday transition: {current.value} -> {target.value}")
    return target


@dataclass(frozen=True, slots=True)
class ProgressDecision:
    action: ProgressAction
    state: WorkdayState
    signature: str
    repeated: bool
    repair_used: bool
    runtime_switch_allowed: bool


def evaluate_page_progress(
    previous_signature: str | None,
    observation: BoundedPageObservation,
    *,
    repair_used: bool,
    submit_started: bool = False,
) -> ProgressDecision:
    """Resolve page progress with at most one repair and fail-closed submit handling."""
    signature = page_signature(observation)
    repeated = previous_signature == signature

    if submit_started:
        return resolve_post_submit(observation, repair_used=repair_used, repeated=repeated)
    if observation.has_manual_gate:
        return ProgressDecision(
            ProgressAction.STOP_MANUAL,
            WorkdayState.MANUAL_REVIEW,
            signature,
            repeated,
            repair_used,
            True,
        )
    if not repeated:
        return ProgressDecision(
            ProgressAction.CONTINUE,
            state_for_observation(observation),
            signature,
            False,
            repair_used,
            True,
        )
    if not repair_used:
        return ProgressDecision(
            ProgressAction.REPAIR_ONCE,
            state_for_observation(observation),
            signature,
            True,
            True,
            True,
        )
    return ProgressDecision(
        ProgressAction.STOP_STUCK,
        WorkdayState.FAILED_STUCK,
        signature,
        True,
        True,
        True,
    )


def resolve_post_submit(
    observation: BoundedPageObservation,
    *,
    repair_used: bool,
    repeated: bool = False,
    runtime_switch_requested: bool = False,
) -> ProgressDecision:
    """Resolve a real submit observation; runtime switching is always forbidden.

    ``runtime_switch_requested`` is accepted so callers can feed their proposed
    action into the pure decision function.  It never authorizes that action.
    """
    del runtime_switch_requested
    signature = page_signature(observation)
    if observation.has_confirmation or observation.page_kind is WorkdayPageKind.CONFIRMATION:
        return ProgressDecision(
            ProgressAction.CONTINUE,
            WorkdayState.APPLIED,
            signature,
            repeated,
            repair_used,
            False,
        )
    if observation.repairable_validation and not repair_used and not observation.has_manual_gate:
        return ProgressDecision(
            ProgressAction.REPAIR_ONCE,
            WorkdayState.FORM,
            signature,
            repeated,
            True,
            False,
        )
    if observation.invalid_count > 0 and not observation.has_manual_gate:
        return ProgressDecision(
            ProgressAction.STOP_VALIDATION,
            WorkdayState.FAILED_VALIDATION,
            signature,
            repeated,
            repair_used,
            False,
        )
    return ProgressDecision(
        ProgressAction.MARK_UNCERTAIN,
        WorkdayState.SUBMISSION_UNCERTAIN,
        signature,
        repeated,
        repair_used,
        False,
    )


def runtime_switch_allowed(*, submit_started: bool) -> bool:
    """Expose the no-runtime-switch submission boundary as a pure predicate."""
    return not submit_started

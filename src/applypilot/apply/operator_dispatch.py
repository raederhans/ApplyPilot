"""Bounded same-process dispatch for exact operator resume requests."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from applypilot.apply.operator_commands import (
    OperatorCommandError,
    OperatorCommandResult,
)
from applypilot.apply.operator_runtime import OperatorRuntime, ResumeOwner

OperatorWaitStatus = Literal["resumed", "blocked", "expired", "stopped", "lease_lost"]


@dataclass(frozen=True, slots=True)
class OperatorWaitResult:
    """One bounded wait outcome; none of these states grants Submit authority."""

    status: OperatorWaitStatus
    command_result: OperatorCommandResult | None = None


def wait_for_requested_resume(
    connection: sqlite3.Connection,
    *,
    exception_id: str,
    request_id: str,
    resume_owner: ResumeOwner,
    heartbeat: Callable[[], bool],
    stop_wait: Callable[[float], bool],
    timeout_seconds: float,
    poll_seconds: float = 1.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> OperatorWaitResult:
    """Wait for one durable request while the original worker owns the page.

    The caller supplies the only legal same-process owner.  Every poll first
    renews and revalidates the exact attempt/browser binding.  Timeout, stop,
    lease loss, or an unverified child expires the old exception/request pair
    so the HumanResponse can never be replayed against a new page.
    """
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be an explicit sqlite3 connection")
    if not exception_id.strip() or not request_id.strip():
        raise ValueError("exception_id and request_id are required")
    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("operator wait and poll durations must be positive")
    runtime = OperatorRuntime(connection, resume_owner=resume_owner)
    deadline = monotonic() + timeout_seconds

    while True:
        if not heartbeat():
            runtime.expire_resume_request(exception_id, request_id=request_id)
            return OperatorWaitResult("lease_lost")
        remaining = deadline - monotonic()
        if remaining <= 0:
            runtime.expire_resume_request(exception_id, request_id=request_id)
            return OperatorWaitResult("expired")
        if stop_wait(0.0):
            runtime.expire_resume_request(exception_id, request_id=request_id)
            return OperatorWaitResult("stopped")
        command = runtime.load_requested_resume(exception_id)
        if command is not None:
            result = runtime.resume(command, request_id=request_id)
            if result.resolved:
                return OperatorWaitResult("resumed", result)
            runtime.expire_resume_request(exception_id, request_id=request_id)
            return OperatorWaitResult("blocked", result)
        if stop_wait(min(poll_seconds, remaining)):
            runtime.expire_resume_request(exception_id, request_id=request_id)
            return OperatorWaitResult("stopped")


__all__ = [
    "OperatorCommandError",
    "OperatorWaitResult",
    "OperatorWaitStatus",
    "wait_for_requested_resume",
]

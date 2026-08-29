from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

import pytest

from applypilot.apply.human_handoff import (
    HumanResponseRef,
    append_human_response,
    fresh_resume_context,
    load_human_response,
)


def _response(ref: str = "response-store://request-1") -> HumanResponseRef:
    return HumanResponseRef(
        request_id="request-1",
        response_ref=ref,
        response_digest=hashlib.sha256(b"approved-choice-A").hexdigest(),
        response_type="screening_answer",
        resolved_by="human:user",
        resolved_at=datetime(2026, 8, 29, tzinfo=UTC),
    )


def test_human_response_is_append_only_idempotent_and_reference_only() -> None:
    connection = sqlite3.connect(":memory:")
    response = _response()
    assert append_human_response(connection, response) is True
    assert append_human_response(connection, response) is False
    loaded = load_human_response(connection, "request-1")
    assert loaded == response
    context = fresh_resume_context(
        connection,
        parent_run_id="run-1",
        checkpoint_ref="checkpoint://1",
        request_id="request-1",
    )
    assert context["resume_mode"] == "fresh_agent_turn"
    assert "approved-choice-A" not in str(context)
    with pytest.raises(ValueError, match="reused"):
        append_human_response(connection, _response("response-store://different"))
    assert connection.execute("SELECT COUNT(*) FROM agent_human_responses").fetchone()[0] == 1
    with pytest.raises(LookupError, match="not found"):
        fresh_resume_context(
            connection,
            parent_run_id="run-1",
            checkpoint_ref="checkpoint://1",
            request_id="missing",
        )

"""Real-process crash/recovery tests for the semantic resume-write journal.

These tests intentionally use separate Python OS processes and a test-local
SQLite database.  They prove the durable journal prevents a restarted process
from reclaiming the initial browser write after the first process disappears.
They do not imply that a production browser lease or an ATS upload itself has
been reconciled.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_CHILD = r'''
import json
import os
import sqlite3
import sys

from applypilot.storage.semantic_browser_writes import (
    OPERATION_KIND,
    SemanticWriteClaims,
    begin_operation,
    claim_dispatch,
    get_operation,
    mark_effect_observed,
    mark_verified,
    park_side_effect_unknown,
)


def claims():
    return SemanticWriteClaims(
        operation_id="semantic-process-op",
        operation_digest="a" * 64,
        actor_id="application:attempt-process",
        attempt_id="attempt-process",
        provider="workday",
        operation_kind=OPERATION_KIND,
        adapter_version="resume-upload-driver/v1",
        application_binding_hash="b" * 64,
        page_id="application:attempt-process",
        page_lease_id="lease-process",
        page_lease_epoch=2,
        expected_page_epoch=3,
        artifact_sha256="c" * 64,
        artifact_size=42,
        material_binding_hash="d" * 64,
        policy_contract_version="semantic-browser-write/v1",
        policy_digest="e" * 64,
        expected_postcondition_digest="f" * 64,
    )


database, phase = sys.argv[1:]
connection = sqlite3.connect(database, timeout=5)
if phase in {"crash-after-claim", "crash-after-effect"}:
    begin_operation(connection, claims())
    claimed = claim_dispatch(
        connection, "semantic-process-op", expected_dispatch_count=0
    )
    if claimed is None:
        raise RuntimeError("first process could not claim initial dispatch")
    if phase == "crash-after-effect":
        mark_effect_observed(connection, "semantic-process-op")
    connection.close()
    os._exit(86)

record = get_operation(connection, "semantic-process-op")
if record is None:
    raise RuntimeError("restart did not find durable operation")
initial_claim = claim_dispatch(
    connection, "semantic-process-op", expected_dispatch_count=0
)
if phase == "recover-unknown":
    replay_claim = claim_dispatch(
        connection,
        "semantic-process-op",
        expected_dispatch_count=1,
        allow_replay=True,
    )
    recovered = park_side_effect_unknown(
        connection,
        "semantic-process-op",
        reason_code="process_disappeared_after_dispatch",
    )
elif phase == "recover-effect":
    replay_claim = claim_dispatch(
        connection,
        "semantic-process-op",
        expected_dispatch_count=1,
        allow_replay=True,
    )
    recovered = mark_verified(
        connection,
        "semantic-process-op",
        resulting_page_epoch=4,
    )
else:
    raise RuntimeError("unknown recovery phase")
print(json.dumps({
    "before_state": record.state,
    "before_dispatch_count": record.dispatch_count,
    "initial_claimed": initial_claim is not None,
    "replay_claimed": replay_claim is not None,
    "state": recovered.state,
    "dispatch_count": recovered.dispatch_count,
    "effect_observed": recovered.effect_observed,
    "reason_code": recovered.reason_code,
}), flush=True)
'''


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source = str(Path(__file__).resolve().parents[1] / "src")
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source if not inherited else f"{source}{os.pathsep}{inherited}"
    )
    return environment


def _run_child(database: Path, phase: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _CHILD, str(database), phase],
        capture_output=True,
        check=False,
        cwd=Path(__file__).resolve().parents[1],
        env=_child_environment(),
        text=True,
        timeout=15,
    )


def _crash_then_recover(database: Path, crash_phase: str, recovery_phase: str) -> dict[str, object]:
    crashed = _run_child(database, crash_phase)
    assert crashed.returncode == 86, crashed.stderr

    recovered = _run_child(database, recovery_phase)
    assert recovered.returncode == 0, recovered.stderr
    return json.loads(recovered.stdout)


def test_process_restart_after_dispatch_parks_unknown_effect_without_retransmit(tmp_path: Path) -> None:
    outcome = _crash_then_recover(
        tmp_path / "semantic-dispatch-crash.db",
        "crash-after-claim",
        "recover-unknown",
    )

    assert outcome == {
        "before_state": "started",
        "before_dispatch_count": 1,
        "initial_claimed": False,
        "replay_claimed": False,
        "state": "parked_side_effect_unknown",
        "dispatch_count": 1,
        "effect_observed": False,
        "reason_code": "process_disappeared_after_dispatch",
    }


def test_process_restart_after_observed_effect_is_recoverable_without_replay(tmp_path: Path) -> None:
    outcome = _crash_then_recover(
        tmp_path / "semantic-effect-crash.db",
        "crash-after-effect",
        "recover-effect",
    )

    assert outcome == {
        "before_state": "effect_observed",
        "before_dispatch_count": 1,
        "initial_claimed": False,
        "replay_claimed": False,
        "state": "verified",
        "dispatch_count": 1,
        "effect_observed": True,
        "reason_code": None,
    }

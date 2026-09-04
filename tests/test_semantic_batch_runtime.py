from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from applypilot.apply import semantic_batch_runtime as runtime_mod
from applypilot.apply.semantic_batch import (
    BatchControlDescriptor,
    BatchPageBinding,
    BrowserPageObservation,
    BrowserResourceIdentity,
    SemanticPatch,
)
from applypilot.apply.semantic_batch_runtime import (
    SemanticBatchRuntimeRequest,
    run_production_semantic_batch,
)
from applypilot.storage import semantic_patch_batches as journal

URL = "https://tenant.wd5.myworkdayjobs.com/apply/REQ-1"
SIGNATURE = "a" * 64


class FakeProductionAdapter:
    provider = "workday"
    adapter_version = "fake-batch/v1"

    def __init__(
        self,
        *,
        classification: str = "routine",
        fail_at: int | None = None,
        drift_at_observation: int | None = None,
        page_url: str = URL,
        page_epoch: int = 4,
    ) -> None:
        self.classification = classification
        self.fail_at = fail_at
        self.drift_at_observation = drift_at_observation
        self.page_url = page_url
        self.page_epoch = page_epoch
        self.apply_calls: list[str] = []
        self._effect_count = 0
        self._sink = lambda: None
        self._observation_count = 0

    @property
    def effect_count(self) -> int:
        return self._effect_count

    def bind_effect_sink(self, sink) -> None:
        self._sink = sink

    def observe_page(self) -> BrowserPageObservation:
        self._observation_count += 1
        signature = "b" * 64 if self.drift_at_observation == self._observation_count else SIGNATURE
        return BrowserPageObservation(self.page_url, (), signature, self.page_epoch)

    def control_for(self, field_semantic: str) -> BatchControlDescriptor:
        return BatchControlDescriptor(
            control_id=f"control:{field_semantic}",
            field_semantic=field_semantic,
            classification=self.classification,
            page=BrowserPageObservation(
                self.page_url,
                (),
                SIGNATURE,
                self.page_epoch,
            ),
        )

    def apply_routine_control(self, control, _value: str) -> None:
        next_effect = self._effect_count + 1
        if self.fail_at == next_effect:
            raise RuntimeError("adapter failed before effect")
        self.apply_calls.append(control.field_semantic)
        self._effect_count = next_effect
        self._sink()

    def pristine(self) -> bool:
        return self._effect_count == 0


def _request(tmp_path: Path, *, mode: str = "canary") -> SemanticBatchRuntimeRequest:
    return SemanticBatchRuntimeRequest(
        mode=mode,  # type: ignore[arg-type]
        attempt_id="attempt-1",
        actor_id="application:attempt-1",
        provider="workday",
        adapter_version="fake-batch/v1",
        page_binding=BatchPageBinding(URL, (), SIGNATURE, 4),
        page_id="application:attempt-1",
        page_lease_id="page-lease-1",
        page_lease_epoch=1,
        resources=BrowserResourceIdentity(
            str(tmp_path / "applypilot.db"),
            str(tmp_path / "profile"),
            9432,
        ),
        patches=(SemanticPatch("email", "private@example.test"), SemanticPatch("phone", "90000000")),
    )


def test_feature_off_has_zero_adapter_storage_or_teardown_behavior(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    adapter = FakeProductionAdapter()
    result = run_production_semantic_batch(
        _request(tmp_path, mode="off"),
        adapter=adapter,
        connection=connection,
        close_resources=lambda: (_ for _ in ()).throw(AssertionError("closed")),
        advance_page=lambda _epoch: (_ for _ in ()).throw(AssertionError("advanced")),
    )

    assert result.status == "off"
    assert adapter.apply_calls == []
    assert connection.execute("SELECT name FROM sqlite_master WHERE name='semantic_patch_batches'").fetchone() is None


def test_shadow_compares_without_effect_and_records_metadata_only(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    adapter = FakeProductionAdapter()
    closed: list[bool] = []
    request = _request(tmp_path, mode="shadow")

    result = run_production_semantic_batch(
        request,
        adapter=adapter,
        connection=connection,
        close_resources=lambda: closed.append(True),
        advance_page=lambda _epoch: (_ for _ in ()).throw(AssertionError("advanced")),
    )

    assert result.status == "shadow_match"
    assert result.legacy_fallback_safe is True
    assert adapter.apply_calls == []
    assert closed == [True]
    assert journal.get_batch(connection, request.batch_id).state == "shadow"  # type: ignore[union-attr]


def test_canary_applies_routine_fields_once_and_replays_without_writes(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    request = _request(tmp_path)
    first_adapter = FakeProductionAdapter()
    advanced: list[int] = []

    first = run_production_semantic_batch(
        request,
        adapter=first_adapter,
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda epoch: advanced.append(epoch) or epoch + 1,
    )
    replay_adapter = FakeProductionAdapter(page_epoch=5)
    replay = run_production_semantic_batch(
        replace(
            request,
            page_binding=replace(request.page_binding, page_epoch=5),
        ),
        adapter=replay_adapter,
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda _epoch: (_ for _ in ()).throw(AssertionError("advanced")),
    )

    assert first.status == "verified"
    assert first_adapter.apply_calls == ["email", "phone"]
    assert advanced == [4]
    assert replay.status == "replayed"
    assert replay_adapter.apply_calls == []
    record = journal.get_batch(connection, request.batch_id)
    assert record is not None and record.state == "verified" and record.effect_count == 2
    bare_payload_digest = hashlib.sha256(
        json.dumps(
            [{"semantic": patch.field_semantic, "value": patch.value} for patch in request.patches],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert record.patch_payload_digest != bare_payload_digest
    durable_dump = " ".join(
        str(value) for row in connection.execute("SELECT * FROM semantic_patch_batches") for value in row
    )
    assert "private@example.test" not in durable_dump
    assert "90000000" not in durable_dump
    assert URL not in durable_dump
    assert str(tmp_path) not in durable_dump


def test_same_semantics_on_different_page_authority_park_without_writes(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(":memory:")
    request = _request(tmp_path)
    run_production_semantic_batch(
        request,
        adapter=FakeProductionAdapter(),
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda epoch: epoch + 1,
    )
    adapter = FakeProductionAdapter()

    result = run_production_semantic_batch(
        replace(
            request,
            page_id="application:attempt-1:next-page",
            page_binding=replace(request.page_binding, page_epoch=5),
        ),
        adapter=adapter,
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda _epoch: (_ for _ in ()).throw(AssertionError("advanced")),
    )

    assert result.status == "parked"
    assert result.reason_code == "prior_semantics_replay_identity_mismatch"
    assert result.legacy_fallback_safe is False
    assert adapter.apply_calls == []


def test_same_lease_and_signature_on_different_url_never_replays(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    request = _request(tmp_path)
    run_production_semantic_batch(
        request,
        adapter=FakeProductionAdapter(),
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda epoch: epoch + 1,
    )
    other_url = "https://tenant.wd5.myworkdayjobs.com/apply/REQ-2"
    adapter = FakeProductionAdapter(page_url=other_url, page_epoch=5)

    result = run_production_semantic_batch(
        replace(
            request,
            page_binding=BatchPageBinding(other_url, (), SIGNATURE, 5),
        ),
        adapter=adapter,
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda _epoch: (_ for _ in ()).throw(AssertionError("advanced")),
    )

    assert result.status == "parked"
    assert result.reason_code == "prior_semantics_replay_identity_mismatch"
    assert adapter.apply_calls == []


def test_same_page_authority_with_different_values_never_replays(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    request = _request(tmp_path)
    run_production_semantic_batch(
        request,
        adapter=FakeProductionAdapter(),
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda epoch: epoch + 1,
    )
    adapter = FakeProductionAdapter(page_epoch=5)

    result = run_production_semantic_batch(
        replace(
            request,
            page_binding=replace(request.page_binding, page_epoch=5),
            patches=(
                SemanticPatch("email", "changed@example.test"),
                SemanticPatch("phone", "90000000"),
            ),
        ),
        adapter=adapter,
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda _epoch: (_ for _ in ()).throw(AssertionError("advanced")),
    )

    assert result.status == "parked"
    assert result.reason_code == "prior_semantics_replay_identity_mismatch"
    assert adapter.apply_calls == []


def test_process_replay_key_change_fails_closed(tmp_path: Path, monkeypatch) -> None:
    connection = sqlite3.connect(":memory:")
    request = _request(tmp_path)
    run_production_semantic_batch(
        request,
        adapter=FakeProductionAdapter(),
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda epoch: epoch + 1,
    )
    monkeypatch.setattr(runtime_mod, "_REPLAY_DIGEST_KEY", b"replacement-process-key" * 2)
    replay_request = replace(
        request,
        page_binding=replace(request.page_binding, page_epoch=5),
    )
    adapter = FakeProductionAdapter(page_epoch=5)

    result = run_production_semantic_batch(
        replay_request,
        adapter=adapter,
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda _epoch: (_ for _ in ()).throw(AssertionError("advanced")),
    )

    assert result.status == "parked"
    assert result.reason_code == "prior_semantics_replay_identity_mismatch"
    assert adapter.apply_calls == []


def test_v1_journal_record_migrates_but_cannot_replay(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE semantic_patch_batch_schema (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL
        );
        INSERT INTO semantic_patch_batch_schema VALUES('semantic_patch_batches', 1);
        CREATE TABLE semantic_patch_batches (
            batch_id TEXT PRIMARY KEY,
            claims_digest TEXT NOT NULL UNIQUE,
            attempt_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            adapter_version TEXT NOT NULL,
            page_id TEXT NOT NULL,
            page_lease_id TEXT NOT NULL,
            page_lease_epoch INTEGER NOT NULL,
            expected_page_epoch INTEGER NOT NULL,
            page_signature TEXT NOT NULL,
            semantics_digest TEXT NOT NULL,
            semantic_count INTEGER NOT NULL,
            state TEXT NOT NULL,
            dispatch_count INTEGER NOT NULL,
            effect_count INTEGER NOT NULL,
            resulting_page_epoch INTEGER,
            reason_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    request = _request(tmp_path)
    semantics_digest = hashlib.sha256(
        json.dumps(
            request.semantics,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    connection.execute(
        "INSERT INTO semantic_patch_batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "semantic-batch:v1",
            "c" * 64,
            request.attempt_id,
            request.actor_id,
            request.provider,
            request.adapter_version,
            request.page_id,
            request.page_lease_id,
            request.page_lease_epoch,
            4,
            SIGNATURE,
            semantics_digest,
            2,
            "verified",
            1,
            2,
            5,
            "verified",
            "2026-09-04T00:00:00+00:00",
            "2026-09-04T00:00:01+00:00",
        ),
    )
    connection.commit()
    adapter = FakeProductionAdapter(page_epoch=5)

    result = run_production_semantic_batch(
        replace(
            request,
            page_binding=replace(request.page_binding, page_epoch=5),
        ),
        adapter=adapter,
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda _epoch: (_ for _ in ()).throw(AssertionError("advanced")),
    )

    assert result.status == "parked"
    assert result.reason_code == "prior_semantics_replay_identity_mismatch"
    assert adapter.apply_calls == []
    assert connection.execute(
        "SELECT version FROM semantic_patch_batch_schema WHERE component=?",
        ("semantic_patch_batches",),
    ).fetchone() == (2,)
    migrated = connection.execute(
        "SELECT replay_key_id,page_identity_digest,patch_payload_digest FROM semantic_patch_batches WHERE batch_id=?",
        ("semantic-batch:v1",),
    ).fetchone()
    assert migrated == (None, None, None)


def test_verified_replay_reconfirms_live_page_binding(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    request = _request(tmp_path)
    run_production_semantic_batch(
        request,
        adapter=FakeProductionAdapter(),
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda epoch: epoch + 1,
    )
    adapter = FakeProductionAdapter(
        drift_at_observation=1,
        page_epoch=5,
    )

    result = run_production_semantic_batch(
        replace(
            request,
            page_binding=replace(request.page_binding, page_epoch=5),
        ),
        adapter=adapter,
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda _epoch: (_ for _ in ()).throw(AssertionError("advanced")),
    )

    assert result.status == "parked"
    assert result.reason_code == "replay_page_authority_unconfirmed"
    assert adapter.apply_calls == []


@pytest.mark.parametrize("classification", ["sensitive", "navigation", "final_submit", "frame"])
def test_privileged_control_classifications_are_zero_write_fallbacks(
    tmp_path: Path,
    classification: str,
) -> None:
    connection = sqlite3.connect(":memory:")
    adapter = FakeProductionAdapter(classification=classification)

    result = run_production_semantic_batch(
        _request(tmp_path),
        adapter=adapter,
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda _epoch: (_ for _ in ()).throw(AssertionError("advanced")),
    )

    assert result.status == "fallback"
    assert result.legacy_fallback_safe is True
    assert adapter.apply_calls == []


def test_signature_drift_after_issue_is_zero_write(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    adapter = FakeProductionAdapter(drift_at_observation=1)

    result = run_production_semantic_batch(
        _request(tmp_path),
        adapter=adapter,
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda _epoch: (_ for _ in ()).throw(AssertionError("advanced")),
    )

    assert result.status == "fallback"
    assert result.effect_count == 0
    assert adapter.apply_calls == []


def test_adapter_failure_after_one_effect_parks_and_never_replays(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    request = _request(tmp_path)
    first_adapter = FakeProductionAdapter(fail_at=2)

    first = run_production_semantic_batch(
        request,
        adapter=first_adapter,
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda _epoch: (_ for _ in ()).throw(AssertionError("advanced")),
    )
    replay_adapter = FakeProductionAdapter()
    replay = run_production_semantic_batch(
        request,
        adapter=replay_adapter,
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda _epoch: (_ for _ in ()).throw(AssertionError("advanced")),
    )

    assert first.status == "parked"
    assert first.effect_count == 1
    assert first.legacy_fallback_safe is False
    assert first_adapter.apply_calls == ["email"]
    assert replay.status == "parked"
    assert replay_adapter.apply_calls == []


def test_adapter_failure_before_effect_can_fallback_only_when_pristine(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    adapter = FakeProductionAdapter(fail_at=1)

    result = run_production_semantic_batch(
        _request(tmp_path),
        adapter=adapter,
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda _epoch: (_ for _ in ()).throw(AssertionError("advanced")),
    )

    assert result.status == "fallback"
    assert result.legacy_fallback_safe is True
    assert adapter.apply_calls == []


def test_page_epoch_cas_failure_after_effect_parks_without_fallback(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    adapter = FakeProductionAdapter()

    result = run_production_semantic_batch(
        _request(tmp_path),
        adapter=adapter,
        connection=connection,
        close_resources=lambda: None,
        advance_page=lambda _epoch: (_ for _ in ()).throw(RuntimeError("stale")),
    )

    assert result.status == "parked"
    assert result.effect_count == 2
    assert result.legacy_fallback_safe is False

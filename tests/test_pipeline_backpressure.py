from __future__ import annotations

import threading

from applypilot import pipeline


def test_streaming_stage_stops_after_bounded_structured_failures(monkeypatch) -> None:
    calls = 0

    def failing_runner(**_kwargs):
        nonlocal calls
        calls += 1
        return {"status": "error: provider unavailable"}

    tracker = pipeline._StageTracker()
    tracker.mark_done("enrich")
    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "score", failing_runner)
    monkeypatch.setattr(pipeline, "_count_pending", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(pipeline, "_STREAM_FAILURE_BUDGET", 3)
    monkeypatch.setattr(pipeline, "_STREAM_BACKOFF_INITIAL", 0.0)

    pipeline._run_stage_streaming("score", tracker, threading.Event())

    result = tracker.get_results()["score"]
    assert calls == 3
    assert result["status"] == "error: provider unavailable"
    assert result["passes"] == 3
    assert result["consecutive_failures"] == 3

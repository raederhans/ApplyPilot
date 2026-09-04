from __future__ import annotations

import socket
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from applypilot.apply.agent_runtime import _current_working_set_bytes
from applypilot.apply.application_sessions import (
    ApplicationSupervisor,
    BrowserWorkerProcess,
    DisabledResponsesRuntimeAdapter,
    EndpointUnavailable,
    LoopbackHttpEndpointManager,
    LoopbackHttpEndpointSpec,
    LoopbackPortReservation,
    PersistentSessionError,
    PerTurnStdioEndpointManager,
    StaleBrowserGeneration,
    SubprocessEndpointManager,
)
from applypilot.apply.browser_broker import BrowserBroker
from applypilot.apply.contracts import application_actor_id
from applypilot.apply.runtime_namespace import RuntimeNamespace

_FAKE_HTTP_CHILD = """
import socket
import sys

port = int(sys.argv[sys.argv.index('--port') + 1])
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(('127.0.0.1', port))
server.listen()
while True:
    connection, _address = server.accept()
    connection.close()
"""


def _socket_accepting(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.05):
            return True
    except OSError:
        return False


def _fake_http_spec(reservation: LoopbackPortReservation) -> LoopbackHttpEndpointSpec:
    return LoopbackHttpEndpointSpec(
        worker_id=1,
        port=reservation.port,
        cdp_port=9333,
        launcher=(sys.executable, "-c", _FAKE_HTTP_CHILD),
    )


@dataclass
class FakeBrowserProcess:
    pid: int
    returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


@dataclass
class FakeBrowserHarness:
    launches: int = 0
    cleanups: int = 0
    opens: int = 0
    closes: int = 0
    fail_next_open: bool = False

    def launch(self, *_args, **_kwargs) -> FakeBrowserProcess:
        self.launches += 1
        return FakeBrowserProcess(pid=10_000 + self.launches)

    def cleanup(self, _worker_id: int, process: FakeBrowserProcess | None) -> None:
        self.cleanups += 1
        if process is not None:
            process.returncode = 0

    def open_target(self, _port: int, _url: str) -> set[str]:
        if self.fail_next_open:
            self.fail_next_open = False
            raise RuntimeError("synthetic target failure")
        self.opens += 1
        return {f"target-{self.opens}"}

    def close_targets(self, _port: int, targets: set[str]) -> None:
        assert len(targets) == 1
        self.closes += 1


def _worker(
    tmp_path: Path,
    harness: FakeBrowserHarness,
    *,
    max_applications: int = 8,
    rss_reader=None,
) -> BrowserWorkerProcess:
    return BrowserWorkerProcess(
        worker_id=1,
        port=9333,
        run_id="run-1",
        namespace_root=tmp_path,
        launch_browser=harness.launch,
        cleanup_browser=harness.cleanup,
        open_target=harness.open_target,
        close_targets=harness.close_targets,
        endpoint_manager=PerTurnStdioEndpointManager(1),
        rss_reader=rss_reader,
        max_applications=max_applications,
        max_rss_bytes=500,
    )


def _supervisor(
    worker: BrowserWorkerProcess,
    broker: BrowserBroker,
    attempt_id: str,
    releases: list[str],
) -> ApplicationSupervisor:
    actor_id = application_actor_id(attempt_id)
    supervisor = ApplicationSupervisor(
        worker,
        attempt_id=attempt_id,
        actor_id=actor_id,
        start_url=f"https://example.test/{attempt_id}",
        browser_runtime="edge",
        headless=True,
        release_browser_authority=lambda: (
            broker.release_scope("worker:1"),
            releases.append(attempt_id),
        ),
        session_id_factory=lambda: f"session-{attempt_id}",
    )
    supervisor.bind_browser_authority(
        broker.acquire_bundle(
            profile_id=f"edge:worker:1:{attempt_id}",
            page_id=f"page:{attempt_id}",
            owner_id=actor_id,
            scope_id="worker:1",
            attempt_id=attempt_id,
            runtime_id="codex:edge:9333",
        )
    )
    return supervisor


def test_one_worker_reuses_browser_but_isolates_two_applications(tmp_path: Path) -> None:
    harness = FakeBrowserHarness()
    worker = _worker(tmp_path, harness)
    broker = BrowserBroker()
    releases: list[str] = []

    first = _supervisor(worker, broker, "attempt-1", releases)
    first_context = first.context_bundle(
        namespace=RuntimeNamespace(tmp_path, "run-1", "turn-1", "profile-1"),
        phase="prepare",
        runtime_backend="codex-cli",
    )
    first.close_application()
    one_application_metrics = worker.metrics()

    second = _supervisor(worker, broker, "attempt-2", releases)
    second_context = second.context_bundle(
        namespace=RuntimeNamespace(tmp_path, "run-1", "turn-2", "profile-2"),
        phase="prepare",
        runtime_backend="codex-cli",
    )
    second.close_application()
    two_application_metrics = worker.metrics()

    assert harness.launches == 1
    assert harness.cleanups == 0
    assert harness.opens == harness.closes == 2
    assert releases == ["attempt-1", "attempt-2"]
    assert first_context.application_session_id != second_context.application_session_id
    assert first_context.actor_id != second_context.actor_id
    assert first_context.root_target_ids != second_context.root_target_ids
    assert first_context.namespace.output_root != second_context.namespace.output_root
    assert first_context.browser_generation == second_context.browser_generation == 1
    assert first_context.endpoint.reusable is False
    assert one_application_metrics == {
        "schema_version": 1,
        "browser_generation": 1,
        "browser_starts": 1,
        "applications_started": 1,
        "applications_completed": 1,
        "browser_reuses": 0,
    }
    assert two_application_metrics["browser_starts"] == 1
    assert two_application_metrics["applications_started"] == 2
    assert two_application_metrics["applications_completed"] == 2
    assert two_application_metrics["browser_reuses"] == 1

    worker.close()
    assert harness.cleanups == 1


def test_worker_accepts_clean_edge_bootstrap_exit_when_cdp_is_live(
    tmp_path: Path,
) -> None:
    harness = FakeBrowserHarness()
    cdp_live = True
    harness_process = FakeBrowserProcess(pid=10_001, returncode=0)
    worker = BrowserWorkerProcess(
        worker_id=1,
        port=9333,
        run_id="run-edge-bootstrap",
        namespace_root=tmp_path,
        launch_browser=lambda *_args, **_kwargs: harness_process,
        cleanup_browser=harness.cleanup,
        open_target=harness.open_target,
        close_targets=harness.close_targets,
        endpoint_manager=PerTurnStdioEndpointManager(1),
        browser_health_probe=lambda: cdp_live,
    )
    supervisor = _supervisor(worker, BrowserBroker(), "attempt-edge-bootstrap", [])

    assert supervisor.process is harness_process
    assert worker.heartbeat(expected_generation=1).transport == "stdio-per-turn"

    supervisor.close_application()
    worker.close()


def test_worker_rolls_after_task_limit_and_rejects_stale_generation(tmp_path: Path) -> None:
    harness = FakeBrowserHarness()
    worker = _worker(tmp_path, harness, max_applications=1)
    broker = BrowserBroker()
    releases: list[str] = []

    first = _supervisor(worker, broker, "attempt-1", releases)
    first_generation = worker.generation
    first.close_application()
    second = _supervisor(worker, broker, "attempt-2", releases)

    assert harness.launches == 2
    assert harness.cleanups == 1
    assert worker.generation == first_generation + 1
    with pytest.raises(StaleBrowserGeneration):
        worker.heartbeat(expected_generation=first_generation)

    second.close_application()
    worker.close()


def test_browser_crash_rolls_before_next_application(tmp_path: Path) -> None:
    harness = FakeBrowserHarness()
    worker = _worker(tmp_path, harness)
    broker = BrowserBroker()
    releases: list[str] = []

    first = _supervisor(worker, broker, "attempt-1", releases)
    crashed = first.process
    first.close_application()
    crashed.returncode = 17
    second = _supervisor(worker, broker, "attempt-2", releases)

    assert harness.launches == 2
    assert harness.cleanups == 1
    assert second.process.pid != crashed.pid
    second.close_application()
    worker.close()


def test_target_exception_discards_generation_before_retry(tmp_path: Path) -> None:
    harness = FakeBrowserHarness(fail_next_open=True)
    worker = _worker(tmp_path, harness)
    broker = BrowserBroker()

    with pytest.raises(RuntimeError, match="synthetic target failure"):
        _supervisor(worker, broker, "attempt-1", [])
    second = _supervisor(worker, broker, "attempt-2", [])

    assert harness.launches == 2
    assert harness.cleanups == 1
    second.close_application()
    worker.close()


def test_submit_started_forbids_runtime_restart(tmp_path: Path) -> None:
    harness = FakeBrowserHarness()
    worker = _worker(tmp_path, harness)
    broker = BrowserBroker()
    supervisor = _supervisor(worker, broker, "attempt-1", [])
    supervisor.mark_submit_started()

    with pytest.raises(PersistentSessionError, match="submit_started"):
        supervisor.restart_browser("cloak", headless=True)

    supervisor.close_application()
    worker.close()


def test_local_subprocess_endpoint_reuses_and_restarts_generation() -> None:
    manager = SubprocessEndpointManager(
        (
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        ),
        endpoint_id="fake-local-endpoint",
        transport="local-test",
        address="process-only",
        ready_probe=lambda process: process.poll() is None,
    )
    try:
        first = manager.start()
        reused = manager.start()
        restarted = manager.restart()

        assert reused == first
        assert restarted.generation == first.generation + 1
        assert restarted.process_id != first.process_id
    finally:
        manager.close()
    with pytest.raises(EndpointUnavailable):
        manager.health()


def test_responses_runtime_seam_is_default_off(tmp_path: Path) -> None:
    adapter = DisabledResponsesRuntimeAdapter()
    assert adapter.enabled is False
    with pytest.raises(EndpointUnavailable, match="disabled"):
        adapter.run(None)  # type: ignore[arg-type]


def test_rss_limit_rolls_at_application_boundary(tmp_path: Path) -> None:
    harness = FakeBrowserHarness()
    rss = {10_001: 0}
    worker = _worker(tmp_path, harness, rss_reader=lambda pid: rss.get(pid, 0))
    broker = BrowserBroker()
    releases: list[str] = []

    first = _supervisor(worker, broker, "attempt-1", releases)
    first.close_application()
    rss[10_001] = 501
    second = _supervisor(worker, broker, "attempt-2", releases)

    assert harness.launches == 2
    assert harness.cleanups == 1
    second.close_application()
    worker.close()


def test_rss_limit_uses_current_working_set_not_historical_peak(
    tmp_path: Path,
) -> None:
    harness = FakeBrowserHarness()
    counters = type(
        "Counters",
        (),
        {"WorkingSetSize": 499, "PeakWorkingSetSize": 501},
    )()
    worker = _worker(
        tmp_path,
        harness,
        rss_reader=lambda _pid: _current_working_set_bytes(counters),
    )
    broker = BrowserBroker()
    releases: list[str] = []

    first = _supervisor(worker, broker, "attempt-current-rss-1", releases)
    first.close_application()
    second = _supervisor(worker, broker, "attempt-current-rss-2", releases)

    assert harness.launches == 1
    assert harness.cleanups == 0
    second.close_application()
    worker.close()


def test_loopback_http_spec_renders_pinned_shared_context_command(tmp_path: Path) -> None:
    reservation = LoopbackPortReservation.reserve(tmp_path, worker_id=2)
    try:
        spec = LoopbackHttpEndpointSpec(
            worker_id=2,
            port=reservation.port,
            cdp_port=9444,
            launcher=("npx", "-y"),
        )
        command = spec.command()

        assert command[:3] == ("npx", "-y", "@playwright/mcp@0.0.79")
        assert command[command.index("--host") + 1] == "127.0.0.1"
        assert command[command.index("--port") + 1] == str(reservation.port)
        assert command[command.index("--cdp-endpoint") + 1] == (
            "http://127.0.0.1:9444"
        )
        assert "--shared-browser-context" in command
        assert spec.url == f"http://127.0.0.1:{reservation.port}/mcp"
    finally:
        reservation.release()


def test_loopback_http_child_reuses_restarts_and_releases_reservation(
    tmp_path: Path,
) -> None:
    reservation = LoopbackPortReservation.reserve(tmp_path, worker_id=1)
    lock_path = reservation.lock_path
    probe_calls: list[tuple[int, int]] = []

    def owner_probe(process, host: str, port: int, generation: int) -> bool:
        probe_calls.append((process.pid, generation))
        return process.poll() is None and _socket_accepting(host, port)

    manager = LoopbackHttpEndpointManager(
        _fake_http_spec(reservation),
        reservation,
        owner_probe=owner_probe,
    )
    first = manager.start()
    reused = manager.start()
    manager.close()
    assert not _socket_accepting("127.0.0.1", reservation.port)
    restarted = manager.start()

    assert reused == first
    assert first.generation == 1
    assert restarted.generation == 2
    assert restarted.process_id != first.process_id
    assert first.address == f"http://127.0.0.1:{reservation.port}/mcp"
    assert first.transport == "streamable-http"
    assert first.reusable is True
    assert {generation for _pid, generation in probe_calls} == {1, 2}

    manager.shutdown()
    assert not lock_path.exists()
    assert not _socket_accepting("127.0.0.1", reservation.port)
    with pytest.raises(EndpointUnavailable, match="shut down"):
        manager.start()


def test_default_owner_probe_attests_exact_local_child_pid(tmp_path: Path) -> None:
    reservation = LoopbackPortReservation.reserve(tmp_path, worker_id=1)
    manager = LoopbackHttpEndpointManager(
        _fake_http_spec(reservation),
        reservation,
    )
    try:
        descriptor = manager.start()
        assert descriptor.process_id is not None
        assert manager.health() == descriptor
    finally:
        manager.shutdown()


def test_occupied_reserved_port_cannot_masquerade_as_endpoint_child(
    tmp_path: Path,
) -> None:
    reservation = LoopbackPortReservation.reserve(tmp_path, worker_id=1)
    launches: list[object] = []
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", reservation.port))
    occupied.listen()
    manager = LoopbackHttpEndpointManager(
        _fake_http_spec(reservation),
        reservation,
        owner_probe=lambda *_args: True,
        popen_factory=lambda *_args, **_kwargs: launches.append(object()),  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(EndpointUnavailable, match="already occupied"):
            manager.start()
        assert launches == []
    finally:
        occupied.close()
        manager.shutdown()


def test_browser_heartbeat_rejects_restarted_http_endpoint_generation(
    tmp_path: Path,
) -> None:
    reservation = LoopbackPortReservation.reserve(tmp_path, worker_id=1)
    manager = LoopbackHttpEndpointManager(
        _fake_http_spec(reservation),
        reservation,
        owner_probe=lambda process, host, port, _generation: (
            process.poll() is None and _socket_accepting(host, port)
        ),
    )
    harness = FakeBrowserHarness()
    worker = BrowserWorkerProcess(
        worker_id=1,
        port=9333,
        run_id="run-http",
        namespace_root=tmp_path,
        launch_browser=harness.launch,
        cleanup_browser=harness.cleanup,
        open_target=harness.open_target,
        close_targets=harness.close_targets,
        endpoint_manager=manager,
    )
    broker = BrowserBroker()
    supervisor = _supervisor(worker, broker, "attempt-http", [])
    browser_generation = worker.generation
    manager.restart()

    with pytest.raises(StaleBrowserGeneration, match="endpoint identity changed"):
        worker.heartbeat(expected_generation=browser_generation)

    supervisor.close_application()
    worker.close()


def test_one_worker_reuses_http_endpoint_across_two_application_turns(
    tmp_path: Path,
) -> None:
    reservation = LoopbackPortReservation.reserve(tmp_path, worker_id=1)
    manager = LoopbackHttpEndpointManager(
        _fake_http_spec(reservation),
        reservation,
        owner_probe=lambda process, host, port, _generation: (
            process.poll() is None and _socket_accepting(host, port)
        ),
    )
    harness = FakeBrowserHarness()
    worker = BrowserWorkerProcess(
        worker_id=1,
        port=9333,
        run_id="run-http-reuse",
        namespace_root=tmp_path,
        launch_browser=harness.launch,
        cleanup_browser=harness.cleanup,
        open_target=harness.open_target,
        close_targets=harness.close_targets,
        endpoint_manager=manager,
    )
    broker = BrowserBroker()
    first = _supervisor(worker, broker, "attempt-http-1", [])
    first_endpoint = worker.endpoint
    first.close_application()
    second = _supervisor(worker, broker, "attempt-http-2", [])
    second_endpoint = worker.endpoint

    assert first_endpoint == second_endpoint
    assert first_endpoint.process_id is not None
    assert harness.launches == 1
    assert worker.metrics()["browser_reuses"] == 1

    second.close_application()
    worker.close()

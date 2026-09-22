from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from applypilot.apply.browser_context_runtime import (
    BrowserContextFeature,
    BrowserStateScope,
    HotBrowserContextRuntime,
    ScopedBrowserState,
)
from applypilot.apply.contracts import application_actor_id


class _ShadowContext:
    """No-I/O Context used only to exercise the production ownership graph."""

    pages: list[Any]
    service_workers: list[Any]

    def __init__(self) -> None:
        self.pages = []
        self.service_workers = []

    def new_page(self) -> Any:
        raise RuntimeError("Runtime Cell shadow contexts have no page authority")

    def close(self) -> None:
        self.pages.clear()
        self.service_workers.clear()


class _ShadowBrowser:
    """Logical browser owner with no CDP, page, navigation, or Submit capability."""

    def __init__(self) -> None:
        self._contexts: list[_ShadowContext] = []

    def new_context(self, **_kwargs: object) -> _ShadowContext:
        context = _ShadowContext()
        self._contexts.append(context)
        return context

    def close(self) -> None:
        for context in self._contexts:
            context.close()
        self._contexts.clear()


@dataclass(slots=True)
class _RuntimeCellShadowSession:
    """One production-shadow Cell binding without browser or effect authority."""

    coordinator: Any
    host: Any
    binding: Any
    active_application: Any = None

    def claim(self, connection: sqlite3.Connection, job: dict, attempt_id: str) -> Any:
        if self.active_application is not None:
            raise RuntimeError("Runtime Cell shadow session already owns an application")
        application_url = str(job.get("application_url") or job.get("url") or "")
        return self.coordinator.claim(
            self.binding,
            application_id=f"runtime-cell-application:{attempt_id}",
            actor_id=application_actor_id(attempt_id),
            attempt_id=attempt_id,
            application_url=application_url,
            connection=connection,
        )

    def open_job(
        self,
        job: dict,
        *,
        agent_stop: Callable[[], None],
        contain_runtime: Callable[[], None],
    ) -> None:
        token = job.get("_runtime_cell_lease")
        if token is None:
            raise RuntimeError("acquired job is missing its Runtime Cell lease")
        application_url = str(job.get("application_url") or job.get("url") or "")
        hostname = urlsplit(application_url).hostname
        if not hostname:
            raise ValueError("Runtime Cell job URL has no hostname")
        scope = BrowserStateScope(
            provider=str(job.get("site") or job.get("source_site") or "shadow"),
            host=hostname,
            account_id="runtime-cell-shadow",
        )
        state = ScopedBrowserState(scope, {"cookies": [], "origins": []})
        self.active_application = self.host.open_claimed_application(
            lease_token=token,
            application_id=f"runtime-cell-application:{job['_attempt_id']}",
            actor_id=application_actor_id(job["_attempt_id"]),
            attempt_id=job["_attempt_id"],
            application_url=application_url,
            scope=scope,
            state=state,
            agent_stop=agent_stop,
            contain_runtime=contain_runtime,
        )
        job["_runtime_cell_shadow"] = {
            "schema_version": "applypilot-runtime-cell-shadow/v1",
            "effective_cells": self.coordinator.decision.effective_cells,
            "production_authority": False,
            "context_evidence": "logical_no_io",
        }

    def close_application(self) -> None:
        if self.active_application is None:
            return
        application = self.active_application
        self.active_application = None
        self.host.close_application(application)

    def close(self) -> None:
        self.close_application()
        self.host.close()


def open_runtime_cell_shadow_session(
    ports: Any,
    settings: Any,
    requested_workers: int,
) -> _RuntimeCellShadowSession | None:
    """Create the single admitted production-shadow Cell, or stay fully off."""

    mode = settings.runtime_cell_mode
    if mode == "off":
        return None
    coordinator = ports.coordinator_factory(
        ports.connection_factory,
        mode=mode,
        requested_workers=requested_workers,
        source_root=ports.source_root,
        manifest_path=settings.runtime_cell_admission_manifest,
    )
    if coordinator.decision.effective_cells != 1 or ports.production_enabled:
        raise RuntimeError("production Runtime Cell shadow must remain hard-gated to one Cell")
    process_id, process_birth_time = ports.process_identity()
    binding = coordinator.register_next(
        cell_index=0,
        runtime_id=(
            f"shadow-worker-{os.getpid()}-{process_birth_time}-{uuid.uuid4().hex}"
        ),
        process_id=process_id,
        process_birth_time=process_birth_time,
    )
    context_runtime = HotBrowserContextRuntime(
        feature=BrowserContextFeature(True),
        launch_browser=_ShadowBrowser,
    )
    host = ports.host_factory(
        coordinator=coordinator,
        binding=binding,
        context_runtime=context_runtime,
        connection_factory=ports.connection_factory,
    )
    return _RuntimeCellShadowSession(coordinator, host, binding)

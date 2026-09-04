"""Feature-gated hot-browser lifecycle with per-application contexts.

This is a deliberately narrow future runtime boundary.  It can keep one
Playwright ``Browser`` process warm, but every application receives a newly
created ``BrowserContext`` and that context is closed before the application
lease is released.  It does not expose submission authority, profile folders,
or a browser-wide cookie jar, and is intentionally not wired into the worker.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit


class BrowserContextRuntimeError(RuntimeError):
    """Base failure for the isolated browser-context lifecycle."""


class BrowserContextFeatureDisabled(BrowserContextRuntimeError):
    """The opt-in lifecycle must never start while its feature is disabled."""


class BrowserContextLeaseError(BrowserContextRuntimeError):
    """A context operation did not present the current exact lease."""


class BrowserContextDrained(BrowserContextRuntimeError):
    """A tainted browser process cannot admit another application."""


def _required(value: object, name: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _hostname(value: object, name: str = "host") -> str:
    host = _required(value, name).casefold().rstrip(".")
    parsed = urlsplit(f"https://{host}")
    if parsed.hostname != host or parsed.port is not None or "/" in host or "@" in host:
        raise ValueError(f"{name} must be an exact hostname")
    return host


def _canonical_payload(value: Mapping[str, object]) -> Mapping[str, object]:
    """Copy storage state without retaining caller-owned mutable objects."""

    try:
        copied = json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError) as exc:
        raise TypeError("storage state must be JSON-compatible") from exc
    if not isinstance(copied, dict):  # Defensive: Mapping inputs serialize as objects.
        raise TypeError("storage state must be an object")
    return MappingProxyType(copied)


def _validate_storage_state(host: str, value: Mapping[str, object]) -> None:
    """Accept only storage that Playwright can attribute to one exact host.

    Browser profile import is intentionally absent.  The caller supplies only
    the serializable state to seed this one newly-created context.  Cookies
    must name the exact host (not a parent-domain cookie); each origin must be
    HTTPS and exact-host as well.
    """

    allowed = {"cookies", "origins"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError("storage state contains unsupported browser-profile fields")
    cookies = value.get("cookies", [])
    origins = value.get("origins", [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise TypeError("storage state cookies and origins must be arrays")
    for cookie in cookies:
        if not isinstance(cookie, Mapping):
            raise TypeError("storage state cookie must be an object")
        _required(cookie.get("name"), "cookie name")
        _required(cookie.get("value"), "cookie value")
        domain = cookie.get("domain")
        url = cookie.get("url")
        if domain is not None and url is not None:
            raise ValueError("cookie cannot contain both domain and URL")
        if domain is not None:
            if _hostname(domain, "cookie domain") != host or str(domain).startswith("."):
                raise ValueError("cookie domain must match the exact application host")
        elif url is not None:
            parsed = urlsplit(_required(url, "cookie url"))
            if parsed.scheme != "https" or (parsed.hostname or "").casefold().rstrip(".") != host:
                raise ValueError("cookie url must match the exact HTTPS application host")
        else:
            raise ValueError("cookie must include an exact domain or URL")
    for origin in origins:
        if not isinstance(origin, Mapping):
            raise TypeError("storage state origin must be an object")
        parsed = urlsplit(_required(origin.get("origin"), "storage origin"))
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold().rstrip(".") != host
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("storage origin must match the exact HTTPS application host")


@dataclass(frozen=True, slots=True)
class BrowserContextFeature:
    """Explicit, dependency-injected admission switch; disabled by default."""

    enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be bool")


@dataclass(frozen=True, slots=True)
class BrowserStateScope:
    """The complete identity required before session state can enter a context."""

    provider: str
    host: str
    account_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _required(self.provider, "provider").casefold())
        object.__setattr__(self, "host", _hostname(self.host))
        object.__setattr__(self, "account_id", _required(self.account_id, "account_id"))


@dataclass(frozen=True, slots=True)
class ScopedBrowserState:
    """Non-serializable, provider/host/account-bound context seed state."""

    scope: BrowserStateScope
    _storage_state: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.scope, BrowserStateScope):
            raise TypeError("scope must be a BrowserStateScope")
        if not isinstance(self._storage_state, Mapping):
            raise TypeError("storage state must be a mapping")
        state = _canonical_payload(self._storage_state)
        _validate_storage_state(self.scope.host, state)
        object.__setattr__(self, "_storage_state", state)

    def __reduce__(self) -> object:
        raise TypeError("scoped browser state cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("scoped browser state cannot be serialized")

    def _playwright_storage_state(self) -> dict[str, object]:
        """Return a fresh copy solely for Playwright ``new_context``."""

        return json.loads(json.dumps(dict(self._storage_state)))


@dataclass(frozen=True, slots=True)
class ApplicationContextLease:
    """Opaque, exact capability for one application-owned browser context."""

    application_id: str
    context_id: str
    nonce: str
    signature: str

    def __reduce__(self) -> object:
        raise TypeError("application context lease cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("application context lease cannot be serialized")


@dataclass(frozen=True, slots=True)
class BrowserContextMetrics:
    """Secret-free lifecycle and residual-resource observations."""

    contexts_created: int
    contexts_closed: int
    active_contexts: int
    pages_before_close: int
    frames_before_close: int
    service_workers_before_close: int
    pages_after_close: int
    frames_after_close: int
    service_workers_after_close: int
    taint_score: int
    drained: bool
    closed: bool


class _PlaywrightContext(Protocol):
    @property
    def pages(self) -> list[Any]: ...

    @property
    def service_workers(self) -> list[Any]: ...

    def new_page(self) -> Any: ...

    def close(self) -> None: ...


class _PlaywrightBrowser(Protocol):
    def new_context(self, **kwargs: object) -> _PlaywrightContext: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class _ActiveContext:
    lease: ApplicationContextLease
    native_context: _PlaywrightContext


class HotBrowserContextRuntime:
    """One hot browser process with a fresh, close-before-release context per app.

    A single taint closes the affected context.  Once the cumulative score
    reaches ``drain_threshold``, every remaining context and the hot browser
    are closed, so the process cannot carry uncertain state into another app.
    """

    def __init__(
        self,
        *,
        feature: BrowserContextFeature,
        launch_browser: Callable[[], _PlaywrightBrowser],
        drain_threshold: int = 3,
    ) -> None:
        if not isinstance(feature, BrowserContextFeature):
            raise TypeError("feature must be a BrowserContextFeature")
        if not isinstance(drain_threshold, int) or isinstance(drain_threshold, bool) or drain_threshold < 1:
            raise ValueError("drain_threshold must be a positive integer")
        self._feature = feature
        self._launch_browser = launch_browser
        self._drain_threshold = drain_threshold
        self._secret = secrets.token_bytes(32)
        self._browser: _PlaywrightBrowser | None = None
        self._active: dict[str, _ActiveContext] = {}
        self._issued_application_ids: set[str] = set()
        self._contexts_created = 0
        self._contexts_closed = 0
        self._pages_before_close = 0
        self._frames_before_close = 0
        self._service_workers_before_close = 0
        self._pages_after_close = 0
        self._frames_after_close = 0
        self._service_workers_after_close = 0
        self._taint_score = 0
        self._drained = False
        self._closed = False
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            if not self._feature.enabled:
                raise BrowserContextFeatureDisabled("browser context runtime feature is disabled")
            if self._drained:
                raise BrowserContextDrained("browser context runtime is drained")
            if self._closed:
                raise BrowserContextRuntimeError("browser context runtime is closed")
            if self._browser is None:
                self._browser = self._launch_browser()

    def open_application(
        self,
        *,
        application_id: str,
        scope: BrowserStateScope,
        state: ScopedBrowserState,
    ) -> ApplicationContextLease:
        """Create a fresh context; no context and no application ID can be reused."""

        with self._lock:
            if not isinstance(scope, BrowserStateScope) or not isinstance(state, ScopedBrowserState):
                raise TypeError("scope and state must use the scoped browser-state contract")
            if state.scope != scope:
                raise BrowserContextRuntimeError("state scope does not match application scope")
            application_id = _required(application_id, "application_id")
            if application_id in self._issued_application_ids:
                raise BrowserContextLeaseError("application_id cannot reuse a browser context")
            self.start()
            assert self._browser is not None
            try:
                native_context = self._browser.new_context(storage_state=state._playwright_storage_state())
            except Exception as exc:
                self._drain_locked()
                raise BrowserContextRuntimeError("browser context creation failed; runtime drained") from exc
            context_id = secrets.token_hex(16)
            unsigned = ApplicationContextLease(application_id, context_id, secrets.token_hex(16), "")
            lease = ApplicationContextLease(
                unsigned.application_id,
                unsigned.context_id,
                unsigned.nonce,
                self._sign(unsigned),
            )
            self._active[context_id] = _ActiveContext(lease, native_context)
            self._issued_application_ids.add(application_id)
            self._contexts_created += 1
            return lease

    def new_page(self, lease: ApplicationContextLease) -> Any:
        """Create a page only inside the exact leased application context."""

        with self._lock:
            return self._active_for(lease).native_context.new_page()

    def close_application(self, lease: ApplicationContextLease) -> None:
        """Close native context before relinquishing its exact opaque lease."""

        with self._lock:
            active = self._active_for(lease)
            self._close_active(active, raise_on_failure=True)

    def taint(self, lease: ApplicationContextLease, *, reason: str, score: int = 1) -> None:
        """Close an uncertain context and drain the whole process at threshold."""

        _required(reason, "reason")
        if not isinstance(score, int) or isinstance(score, bool) or score < 1:
            raise ValueError("score must be a positive integer")
        with self._lock:
            active = self._active_for(lease)
            self._taint_score += score
            self._close_active(active, raise_on_failure=False)
            if self._taint_score >= self._drain_threshold:
                self._drain_locked()

    def drain(self, *, reason: str) -> None:
        """Fail closed by closing every owned context and the browser process."""

        _required(reason, "reason")
        with self._lock:
            self._drain_locked()

    def close(self) -> None:
        """Terminally close this owned process and revoke every context lease."""

        with self._lock:
            if self._closed:
                return
            self._drain_locked()

    @property
    def metrics(self) -> BrowserContextMetrics:
        with self._lock:
            return BrowserContextMetrics(
                contexts_created=self._contexts_created,
                contexts_closed=self._contexts_closed,
                active_contexts=len(self._active),
                pages_before_close=self._pages_before_close,
                frames_before_close=self._frames_before_close,
                service_workers_before_close=self._service_workers_before_close,
                pages_after_close=self._pages_after_close,
                frames_after_close=self._frames_after_close,
                service_workers_after_close=self._service_workers_after_close,
                taint_score=self._taint_score,
                drained=self._drained,
                closed=self._closed,
            )

    def _active_for(self, lease: ApplicationContextLease) -> _ActiveContext:
        if not isinstance(lease, ApplicationContextLease) or not hmac.compare_digest(lease.signature, self._sign(
            ApplicationContextLease(lease.application_id, lease.context_id, lease.nonce, "")
        )):
            raise BrowserContextLeaseError("application context lease signature is invalid")
        try:
            active = self._active[lease.context_id]
        except KeyError as exc:
            raise BrowserContextLeaseError("application context lease is released or unknown") from exc
        if active.lease != lease:
            raise BrowserContextLeaseError("application context lease does not match the active context")
        return active

    def _close_active(self, active: _ActiveContext, *, raise_on_failure: bool) -> None:
        before = self._resource_counts(active.native_context)
        self._pages_before_close += before[0]
        self._frames_before_close += before[1]
        self._service_workers_before_close += before[2]
        try:
            active.native_context.close()
        except Exception as exc:
            # The native object may be indeterminate, but its capability is
            # terminally revoked before the owned browser is drained.
            self._active.pop(active.lease.context_id, None)
            self._drain_locked()
            if raise_on_failure:
                raise BrowserContextRuntimeError("browser context close failed; runtime drained") from exc
            return
        after = self._resource_counts(active.native_context)
        self._pages_after_close += after[0]
        self._frames_after_close += after[1]
        self._service_workers_after_close += after[2]
        self._active.pop(active.lease.context_id, None)
        if any(after):
            # ``close()`` returning is insufficient evidence that this context
            # has released its resources.  The exact lease is already revoked;
            # terminally drain the process so no later application can inherit
            # a residual page, frame, storage partition, or service worker.
            try:
                self._drain_locked()
            except BrowserContextRuntimeError as drain_error:
                if raise_on_failure:
                    raise BrowserContextRuntimeError(
                        "browser context close left residual resources; runtime drained with cleanup failure"
                    ) from drain_error
                return
            if raise_on_failure:
                raise BrowserContextRuntimeError("browser context close left residual resources; runtime drained")
            return
        self._contexts_closed += 1

    @staticmethod
    def _resource_counts(context: _PlaywrightContext) -> tuple[int, int, int]:
        try:
            pages = tuple(context.pages)
            return len(pages), sum(len(page.frames) for page in pages), len(tuple(context.service_workers))
        except Exception:  # noqa: BLE001 - native Playwright/protocol objects have no shared error base.
            # Resource-observation uncertainty is treated as residual state so
            # the caller terminally drains the owned browser process.
            return (1, 1, 1)

    def _drain_locked(self) -> None:
        if self._drained:
            return
        self._drained = True
        close_error: Exception | None = None
        try:
            for active in tuple(self._active.values()):
                self._close_active(active, raise_on_failure=False)
            if self._browser is not None:
                self._browser.close()
        except Exception as exc:  # noqa: BLE001 - terminal containment must survive native close errors.
            close_error = exc
        finally:
            # A terminal drain always revokes all capabilities even if native
            # cleanup is uncertain or its final browser close raised.
            self._active.clear()
            self._browser = None
            self._closed = True
        if close_error is not None:
            raise BrowserContextRuntimeError("browser process close failed; runtime drained") from close_error

    def _sign(self, lease: ApplicationContextLease) -> str:
        payload = json.dumps(
            {
                "application_id": lease.application_id,
                "context_id": lease.context_id,
                "nonce": lease.nonce,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()


__all__ = [
    "ApplicationContextLease",
    "BrowserContextDrained",
    "BrowserContextFeature",
    "BrowserContextFeatureDisabled",
    "BrowserContextLeaseError",
    "BrowserContextMetrics",
    "BrowserContextRuntimeError",
    "BrowserStateScope",
    "HotBrowserContextRuntime",
    "ScopedBrowserState",
]

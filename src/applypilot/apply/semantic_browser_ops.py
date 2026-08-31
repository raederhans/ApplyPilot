"""Read-only semantic browser operations guarded by broker page versions."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from applypilot.apply.browser_broker import (
    BrowserAuthorityDenied,
    BrowserBroker,
    BrowserLeaseBundle,
)


class SemanticBrowserOps:
    """Minimal observation surface; mutation authority intentionally absent."""

    def __init__(
        self,
        broker: BrowserBroker,
        observe_form: Callable[[], Mapping[str, object]],
    ) -> None:
        self._broker = broker
        self._observe_form = observe_form

    def observe_form(self, bundle: BrowserLeaseBundle) -> dict[str, object]:
        self._broker.require_operation(bundle.page_binding, "observe_form")
        observation = dict(self._observe_form())
        self._broker.require_operation(bundle.page_binding, "observe_form")
        return observation

    def apply_form_patch(self, *_args: object, **_kwargs: object) -> None:
        raise BrowserAuthorityDenied(
            "semantic browser ops do not hold page_write authority"
        )

    def upload_artifact(self, *_args: object, **_kwargs: object) -> None:
        raise BrowserAuthorityDenied(
            "semantic browser ops do not hold page_write authority"
        )

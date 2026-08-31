"""Stable, provider-neutral identity for an observed application page.

The binding is an optimistic concurrency token.  It carries no browser-write,
submission, manifest, ledger, or receipt authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass


def _required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


@dataclass(frozen=True, slots=True)
class PageBinding:
    """Lease- and epoch-bound optimistic token for one logical page."""

    page_id: str
    page_lease_id: str
    page_lease_epoch: int
    page_epoch: int
    profile_lease_id: str
    owner_id: str
    attempt_id: str
    runtime_id: str
    schema_version: str = "1"

    def __post_init__(self) -> None:
        for name in (
            "page_id",
            "page_lease_id",
            "profile_lease_id",
            "owner_id",
            "attempt_id",
            "runtime_id",
            "schema_version",
        ):
            _required(getattr(self, name), name)
        if isinstance(self.page_lease_epoch, bool) or self.page_lease_epoch < 1:
            raise ValueError("page_lease_epoch must be a positive integer")
        if isinstance(self.page_epoch, bool) or self.page_epoch < 0:
            raise ValueError("page_epoch must be a non-negative integer")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PageBinding:
        return cls(
            page_id=str(value.get("page_id") or ""),
            page_lease_id=str(value.get("page_lease_id") or ""),
            page_lease_epoch=int(value.get("page_lease_epoch") or 0),
            page_epoch=int(value.get("page_epoch") or 0),
            profile_lease_id=str(value.get("profile_lease_id") or ""),
            owner_id=str(value.get("owner_id") or ""),
            attempt_id=str(value.get("attempt_id") or ""),
            runtime_id=str(value.get("runtime_id") or ""),
            schema_version=str(value.get("schema_version") or "1"),
        )

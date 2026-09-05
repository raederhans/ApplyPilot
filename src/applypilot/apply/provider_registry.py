"""Capability-scoped provider identity and host registry.

Provider detection and browser authority are deliberately separate.  A host
being recognizable as one ATS never grants semantic upload, control-write, or
credential-relay capability unless that exact capability declares the host.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlparse

HostMatchKind = Literal["exact", "suffix"]
UrlCapability = Literal[
    "detection",
    "semantic_upload",
    "control_write",
    "credential_relay",
    "linkedin_external_handoff",
]
ProviderFlag = Literal["application_episode"]


@dataclass(frozen=True, slots=True)
class HostRule:
    """One normalized hostname rule with explicit matching semantics."""

    host: str
    match: HostMatchKind = "suffix"

    def __post_init__(self) -> None:
        normalized = self.host.casefold().strip().strip(".")
        if not normalized or ":" in normalized or "/" in normalized:
            raise ValueError("provider host rule must be a hostname")
        object.__setattr__(self, "host", normalized)

    def matches(self, hostname: object) -> bool:
        normalized = normalize_hostname(hostname)
        if not normalized:
            return False
        if self.match == "exact":
            return normalized == self.host
        return normalized == self.host or normalized.endswith(f".{self.host}")


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Provider facts partitioned by the capability that consumes them."""

    name: str
    detection_hosts: tuple[HostRule, ...] = ()
    semantic_upload_hosts: tuple[HostRule, ...] = ()
    control_write_hosts: tuple[HostRule, ...] = ()
    credential_relay_hosts: tuple[HostRule, ...] = ()
    linkedin_external_handoff_hosts: tuple[HostRule, ...] = ()
    application_episode: bool = False

    def __post_init__(self) -> None:
        normalized = self.name.casefold().strip()
        if not normalized:
            raise ValueError("provider name is required")
        object.__setattr__(self, "name", normalized)

    def hosts_for(self, capability: UrlCapability) -> tuple[HostRule, ...]:
        return {
            "detection": self.detection_hosts,
            "semantic_upload": self.semantic_upload_hosts,
            "control_write": self.control_write_hosts,
            "credential_relay": self.credential_relay_hosts,
            "linkedin_external_handoff": self.linkedin_external_handoff_hosts,
        }[capability]

    def supports(self, capability: UrlCapability | ProviderFlag) -> bool:
        if capability == "application_episode":
            return self.application_episode
        return bool(self.hosts_for(capability))


def _exact(*hosts: str) -> tuple[HostRule, ...]:
    return tuple(HostRule(host, "exact") for host in hosts)


def _suffix(*hosts: str) -> tuple[HostRule, ...]:
    return tuple(HostRule(host, "suffix") for host in hosts)


_PROVIDERS = (
    ProviderDescriptor(
        "greenhouse",
        detection_hosts=_exact("boards.greenhouse.io", "job-boards.greenhouse.io"),
        credential_relay_hosts=_suffix("greenhouse.io", "greenhouse.com"),
    ),
    ProviderDescriptor(
        "lever",
        detection_hosts=_exact("jobs.lever.co", "jobs.eu.lever.co"),
        credential_relay_hosts=_suffix("lever.co"),
    ),
    ProviderDescriptor(
        "ashby",
        detection_hosts=_exact("jobs.ashbyhq.com"),
        credential_relay_hosts=_suffix("ashbyhq.com"),
    ),
    ProviderDescriptor(
        "smartrecruiters",
        detection_hosts=_exact("jobs.smartrecruiters.com"),
        semantic_upload_hosts=_suffix("smartrecruiters.com"),
        control_write_hosts=_suffix("smartrecruiters.com"),
        credential_relay_hosts=_suffix("smartrecruiters.com"),
        application_episode=True,
    ),
    ProviderDescriptor(
        "workday",
        detection_hosts=_suffix("myworkdayjobs.com", "myworkdaysite.com"),
        semantic_upload_hosts=_suffix("myworkdayjobs.com", "myworkdaysite.com"),
        control_write_hosts=_suffix("myworkdayjobs.com", "myworkdaysite.com"),
        credential_relay_hosts=_suffix(
            "myworkdayjobs.com", "workday.com", "workdayjobs.com"
        ),
        application_episode=True,
    ),
    ProviderDescriptor(
        "linkedin",
        detection_hosts=_suffix("linkedin.com"),
        linkedin_external_handoff_hosts=_suffix("linkedin.com"),
    ),
    ProviderDescriptor("bamboohr", credential_relay_hosts=_suffix("bamboohr.com")),
    ProviderDescriptor("icims", credential_relay_hosts=_suffix("icims.com")),
    ProviderDescriptor("jobvite", credential_relay_hosts=_suffix("jobvite.com")),
    ProviderDescriptor("oracle", credential_relay_hosts=_suffix("oraclecloud.com")),
    ProviderDescriptor(
        "successfactors",
        credential_relay_hosts=_suffix("successfactors.com", "successfactors.eu"),
    ),
    ProviderDescriptor("workable", credential_relay_hosts=_suffix("workable.com")),
)

PROVIDER_REGISTRY: Mapping[str, ProviderDescriptor] = MappingProxyType(
    {item.name: item for item in _PROVIDERS}
)


def normalize_hostname(value: object) -> str:
    return str(value or "").casefold().strip().strip(".")


def descriptor(name: object) -> ProviderDescriptor | None:
    return PROVIDER_REGISTRY.get(str(name or "").casefold().strip())


def provider_supports(name: object, capability: UrlCapability | ProviderFlag) -> bool:
    resolved = descriptor(name)
    return bool(resolved and resolved.supports(capability))


def provider_matches_host(
    name: object,
    hostname: object,
    capability: UrlCapability,
) -> bool:
    resolved = descriptor(name)
    return bool(
        resolved
        and any(rule.matches(hostname) for rule in resolved.hosts_for(capability))
    )


def provider_for_host(hostname: object, capability: UrlCapability) -> str | None:
    matches = [
        item.name
        for item in _PROVIDERS
        if any(rule.matches(hostname) for rule in item.hosts_for(capability))
    ]
    return matches[0] if len(matches) == 1 else None


def provider_for_url(value: object, capability: UrlCapability) -> str | None:
    """Resolve one HTTPS provider for one exact capability, or fail closed."""

    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return None
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        return None
    return provider_for_host(parsed.hostname, capability)


def host_supports_credential_relay(hostname: object) -> bool:
    """Recognize a relay host without granting any other provider capability."""

    return provider_for_host(hostname, "credential_relay") is not None


def provider_names(capability: UrlCapability | ProviderFlag) -> frozenset[str]:
    return frozenset(item.name for item in _PROVIDERS if item.supports(capability))

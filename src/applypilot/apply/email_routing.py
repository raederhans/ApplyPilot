"""Provider-neutral routing for mailbox-assisted application work.

This module decides *which* email workflow is available from declared tool
capabilities.  It does not retrieve messages, send mail, or accept secret
values.  Keeping those side effects outside the router makes the same policy
usable by different agent runtimes and MCP providers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

EmailIntent = Literal["verification", "direct_application"]

DEFAULT_MAILBOX_MCP_PACKAGE = "@gongrzhe/server-gmail-autoauth-mcp"
_SAFE_MCP_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_EMAIL = re.compile(r"^[^\s@]+@([^\s@]+)$")
_EMAIL_SEARCH = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")


def _args(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ()
        if stripped.startswith("["):
            parsed = json.loads(stripped)
            if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
                raise ValueError("mailbox MCP argument JSON must be an array of strings")
            return tuple(parsed)
        return tuple(shlex.split(stripped, posix=os.name != "nt"))
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise TypeError("mailbox MCP arguments must be a string or sequence of strings")


@dataclass(frozen=True, slots=True)
class MailboxMcpSpec:
    """Replaceable mailbox MCP process and semantic tool names."""

    server_name: str = "mailbox"
    package: str | None = DEFAULT_MAILBOX_MCP_PACKAGE
    command: str = "npx"
    launcher_args: tuple[str, ...] | None = None
    extra_args: tuple[str, ...] = ()
    search_tool: str = "search_emails"
    read_tool: str = "read_email"
    send_tool: str = "send_email"
    env: Mapping[str, str] = field(default_factory=dict)
    startup_timeout_seconds: int = 60
    tool_timeout_seconds: int = 60
    enabled: bool = True
    source: str = "default"

    def __post_init__(self) -> None:
        names = (self.server_name, self.search_tool, self.read_tool, self.send_tool)
        if not self.command.strip() or not all(_SAFE_MCP_NAME.fullmatch(name) for name in names):
            raise ValueError("mailbox MCP command and server/tool names must be non-empty and safe")
        if self.package is not None and not self.package.strip():
            raise ValueError("mailbox MCP package must be non-empty or None")
        if self.startup_timeout_seconds <= 0 or self.tool_timeout_seconds <= 0:
            raise ValueError("mailbox MCP timeouts must be positive")

    def resolved_launcher_args(self) -> tuple[str, ...]:
        if self.launcher_args is not None:
            return self.launcher_args
        return ("-y",) if self.command.casefold() in {"npx", "npx.cmd"} else ()

    def process_args(self) -> list[str]:
        package = [self.package] if self.package is not None else []
        return [*self.resolved_launcher_args(), *package, *self.extra_args]

    def enabled_tools(self, *, direct_email_send_authorized: bool = False) -> list[str]:
        tools = [self.search_tool, self.read_tool]
        if direct_email_send_authorized:
            tools.append(self.send_tool)
        return tools

    def metadata(self, *, direct_email_send_authorized: bool = False) -> dict[str, object]:
        return {
            "server_name": self.server_name,
            "package": self.package,
            "command": self.command,
            "launcher_args": list(self.resolved_launcher_args()),
            "extra_args": list(self.extra_args),
            "enabled_tools": self.enabled_tools(
                direct_email_send_authorized=direct_email_send_authorized
            ),
            "environment_keys": sorted(self.env),
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "tool_timeout_seconds": self.tool_timeout_seconds,
            "enabled": self.enabled,
            "source": self.source,
        }


def resolve_mailbox_mcp_spec(
    explicit: MailboxMcpSpec | Mapping[str, object] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    configured: Mapping[str, object] | None = None,
) -> MailboxMcpSpec:
    """Resolve explicit > configured > environment > portable defaults."""
    if isinstance(explicit, MailboxMcpSpec):
        return explicit
    environment = os.environ if environ is None else environ
    values: dict[str, object] = dict(configured or {})
    env_values: dict[str, object] = {
        "server_name": environment.get("APPLYPILOT_MAILBOX_MCP_SERVER_NAME"),
        "package": environment.get("APPLYPILOT_MAILBOX_MCP_PACKAGE"),
        "command": environment.get("APPLYPILOT_MAILBOX_MCP_COMMAND"),
        "launcher_args": environment.get("APPLYPILOT_MAILBOX_MCP_LAUNCHER_ARGS"),
        "extra_args": environment.get("APPLYPILOT_MAILBOX_MCP_EXTRA_ARGS"),
        "search_tool": environment.get("APPLYPILOT_MAILBOX_MCP_SEARCH_TOOL"),
        "read_tool": environment.get("APPLYPILOT_MAILBOX_MCP_READ_TOOL"),
        "send_tool": environment.get("APPLYPILOT_MAILBOX_MCP_SEND_TOOL"),
        "startup_timeout_seconds": environment.get("APPLYPILOT_MAILBOX_MCP_STARTUP_TIMEOUT"),
        "tool_timeout_seconds": environment.get("APPLYPILOT_MAILBOX_MCP_TOOL_TIMEOUT"),
        "enabled": environment.get("APPLYPILOT_MAILBOX_MCP_ENABLED"),
    }
    for key, value in env_values.items():
        if value is not None and key not in values:
            values[key] = value
    if explicit is not None:
        values.update(explicit)

    source = (
        "explicit"
        if explicit is not None
        else "configured"
        if configured
        else "environment"
        if any(value is not None for value in env_values.values())
        else "default"
    )
    enabled_value = values.get("enabled", True)
    enabled = (
        enabled_value.strip().casefold() not in {"0", "false", "no", "off"}
        if isinstance(enabled_value, str)
        else bool(enabled_value)
    )
    package_value = values.get("package", DEFAULT_MAILBOX_MCP_PACKAGE)
    package = None if package_value is None else str(package_value).strip() or None
    return MailboxMcpSpec(
        server_name=str(values.get("server_name") or "mailbox"),
        package=package,
        command=str(values.get("command") or "npx"),
        launcher_args=_args(values["launcher_args"]) if "launcher_args" in values else None,
        extra_args=_args(values.get("extra_args")),
        search_tool=str(values.get("search_tool") or "search_emails"),
        read_tool=str(values.get("read_tool") or "read_email"),
        send_tool=str(values.get("send_tool") or "send_email"),
        env={str(key): str(value) for key, value in dict(values.get("env") or {}).items()},
        startup_timeout_seconds=int(values.get("startup_timeout_seconds") or 60),
        tool_timeout_seconds=int(values.get("tool_timeout_seconds") or 60),
        enabled=enabled,
        source=source,
    )


def mailbox_mcp_for_phase(
    spec: MailboxMcpSpec,
    *,
    submission_phase: str,
    direct_email_send_authorized: bool = False,
    verification_resume: bool = False,
) -> MailboxMcpSpec:
    """Disable mailbox startup for ordinary browser-submit turns.

    Prepare turns may need receipt or verification discovery. Submit turns only
    retain the server for an exact direct-email reservation or an observed
    verification-resume flow.
    """
    enabled = spec.enabled and (
        submission_phase.casefold() != "submit"
        or direct_email_send_authorized
        or verification_resume
    )
    return (
        spec
        if enabled == spec.enabled
        else replace(spec, enabled=enabled, source="phase-scoped")
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _direct_email_plan_digest(plan: Mapping[str, object]) -> str:
    fields = {
        "recipient": plan.get("recipient"),
        "subject": plan.get("subject"),
        "body_sha256": plan.get("body_sha256"),
        "attachments": plan.get("attachments"),
        "duplicate_check": plan.get("duplicate_check"),
        "listing_evidence": plan.get("listing_evidence"),
    }
    encoded = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_prepared_email_application(
    plan: object,
    job: Mapping[str, object],
) -> dict[str, object] | None:
    """Bind an agent proposal to official routing evidence and staged bytes."""
    if not isinstance(plan, Mapping) or plan.get("route") != "direct_email":
        return None
    recipient = str(plan.get("recipient") or "").strip().casefold()
    match = _EMAIL.fullmatch(recipient)
    subject = " ".join(str(plan.get("subject") or "").split())
    domain = str(plan.get("recipient_domain") or "").strip().casefold().strip(".")
    listing_evidence = " ".join(str(plan.get("listing_evidence") or "").split())
    body_sha256 = str(plan.get("body_sha256") or "").strip().casefold()
    duplicate = plan.get("duplicate_check")
    runtime_duplicate = job.get("_mailbox_prepare_duplicate_receipt")
    listing_text = "\n".join(
        str(job.get(key) or "") for key in ("full_description", "description")
    )
    listing_recipients = {
        address.casefold() for address in _EMAIL_SEARCH.findall(listing_text)
    }
    listing_source_url = str(job.get("source_url") or job.get("url") or "").strip()
    if (
        match is None
        or not subject
        or plan.get("recipient_source") != "official_listing"
        or domain != match.group(1).casefold().strip(".")
        or not listing_evidence
        or recipient not in listing_evidence.casefold()
        or _SHA256.fullmatch(body_sha256) is None
        or plan.get("attachments_verified") is not True
        or not isinstance(duplicate, Mapping)
        or str(duplicate.get("folder") or "").strip().casefold() != "sent"
        or duplicate.get("completed") is not True
        or duplicate.get("duplicate_found") is not False
        or not isinstance(runtime_duplicate, Mapping)
        or str(runtime_duplicate.get("folder") or "").strip().casefold() != "sent"
        or str(runtime_duplicate.get("recipient") or "").strip().casefold()
        != recipient
        or " ".join(str(runtime_duplicate.get("subject") or "").split())
        != subject
        or runtime_duplicate.get("duplicate_found") is not False
        or runtime_duplicate.get("result_count") != 0
        or _SHA256.fullmatch(
            str(runtime_duplicate.get("query_digest") or "").strip().casefold()
        )
        is None
        or recipient not in listing_recipients
        or not listing_source_url
    ):
        return None

    staged_paths = [
        Path(str(job[key])).expanduser().resolve()
        for key in ("_staged_resume_path", "_staged_cover_letter_path")
        if str(job.get(key) or "").strip()
    ]
    if not staged_paths or any(not path.is_file() for path in staged_paths):
        return None
    attachment_names = plan.get("attachment_names")
    normalized_names = (
        [name.strip() for name in attachment_names]
        if isinstance(attachment_names, list)
        and all(isinstance(name, str) and name.strip() for name in attachment_names)
        else []
    )
    staged_by_name = {path.name: path for path in staged_paths}
    resume_name = Path(str(job.get("_staged_resume_path") or "")).name
    if (
        not normalized_names
        or len(normalized_names) != len(set(normalized_names))
        or resume_name not in normalized_names
        or any(name not in staged_by_name for name in normalized_names)
    ):
        return None
    selected_paths = [staged_by_name[name] for name in normalized_names]
    attachments = [
        {"name": path.name, "path": str(path), "sha256": _sha256_file(path)}
        for path in selected_paths
    ]
    normalized_duplicate = {
        "folder": "sent",
        "completed": True,
        "duplicate_found": False,
        "provider_query_id": str(runtime_duplicate.get("query_digest")),
    }
    return {
        "route": "direct_email",
        "recipient": recipient,
        "recipient_domain": domain,
        "recipient_source": "official_listing",
        "subject": subject,
        "attachment_names": normalized_names,
        "attachments_verified": True,
        "attachments": attachments,
        "body_sha256": body_sha256,
        "duplicate_found": False,
        "duplicate_check": normalized_duplicate,
        "listing_evidence": listing_evidence[:500],
        "listing_source_url": listing_source_url[:1000],
    }


def reserve_direct_email_send(
    job: Mapping[str, object],
    plan: Mapping[str, object],
) -> dict[str, str]:
    """Create an exact-job reservation binding after manifest admission."""
    return {
        "state": "reserved",
        "job_url": str(job.get("url") or "").strip(),
        "attempt_id": str(job.get("_attempt_id") or "").strip(),
        "plan_sha256": _direct_email_plan_digest(plan),
    }


def direct_email_send_is_reserved(
    job: Mapping[str, object],
    *,
    submission_phase: str,
) -> bool:
    """Return whether this exact submit turn may receive a send capability."""
    if submission_phase != "submit":
        return False
    observation = job.get("_browser_observation")
    reservation = job.get("_direct_email_send_reservation")
    if not isinstance(observation, Mapping) or not isinstance(reservation, Mapping):
        return False
    plan = observation.get("email_application")
    if not isinstance(plan, Mapping) or plan.get("route") != "direct_email":
        return False
    job_url = str(job.get("url") or "").strip()
    attempt_id = str(job.get("_attempt_id") or "").strip()
    return bool(
        job_url
        and attempt_id
        and reservation.get("state") == "reserved"
        and str(reservation.get("job_url") or "") == job_url
        and str(reservation.get("attempt_id") or "") == attempt_id
        and str(reservation.get("plan_sha256") or "") == _direct_email_plan_digest(plan)
    )


def normalize_sent_receipt(
    receipt: object,
    plan: Mapping[str, object],
) -> dict[str, object] | None:
    """Validate provider Sent evidence against the exact reserved plan."""
    if not isinstance(receipt, Mapping):
        return None
    folder = str(receipt.get("folder") or "").strip().casefold()
    recipient = str(receipt.get("recipient") or "").strip().casefold()
    subject = " ".join(str(receipt.get("subject") or "").split())
    provider_message_id = str(receipt.get("provider_message_id") or "").strip()
    body_sha256 = str(receipt.get("body_sha256") or "").strip().casefold()
    attachment_names = receipt.get("attachment_names")
    if (
        folder != "sent"
        or recipient != str(plan.get("recipient") or "").strip().casefold()
        or subject != " ".join(str(plan.get("subject") or "").split())
        or not provider_message_id
        or body_sha256 != str(plan.get("body_sha256") or "").strip().casefold()
        or not isinstance(attachment_names, list)
        or [str(name).strip() for name in attachment_names]
        != [str(name).strip() for name in plan.get("attachment_names", [])]
    ):
        return None
    return {
        "folder": "sent",
        "recipient": recipient,
        "subject": subject,
        "attachment_names": [str(name).strip() for name in attachment_names],
        "body_sha256": body_sha256,
        "provider_message_id": provider_message_id[:180],
    }


def mailbox_send_input_matches_plan(
    tool_input: object,
    plan: Mapping[str, object],
) -> bool:
    """Validate the actual send-tool input without retaining its message body."""
    if not isinstance(tool_input, Mapping):
        return False
    raw_recipient = tool_input.get("recipient", tool_input.get("to"))
    if isinstance(raw_recipient, list):
        recipients = [str(value).strip().casefold() for value in raw_recipient]
    else:
        recipients = [str(raw_recipient or "").strip().casefold()]
    subject = " ".join(str(tool_input.get("subject") or "").split())
    body = tool_input.get("body", tool_input.get("message", tool_input.get("content")))
    if not isinstance(body, str):
        return False
    raw_attachments = tool_input.get("attachments", tool_input.get("attachment_paths", []))
    if not isinstance(raw_attachments, list):
        return False
    names = []
    for value in raw_attachments:
        if isinstance(value, Mapping):
            raw_name = value.get("name", value.get("filename", value.get("path")))
        else:
            raw_name = value
        name = Path(str(raw_name or "")).name
        if not name:
            return False
        names.append(name)
    body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return bool(
        recipients == [str(plan.get("recipient") or "").strip().casefold()]
        and subject == " ".join(str(plan.get("subject") or "").split())
        and names == [str(name) for name in plan.get("attachment_names", [])]
        and body_sha256 == str(plan.get("body_sha256") or "").strip().casefold()
    )


def _decoded_tool_values(value: object) -> list[object]:
    """Return JSON-like tool values in memory without retaining raw mail content."""
    values: list[object] = [value]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return values
        values.extend(_decoded_tool_values(decoded))
    elif isinstance(value, Mapping):
        for key in (
            "content",
            "text",
            "result",
            "output",
            "data",
            "messages",
            "message",
        ):
            nested = value.get(key)
            if nested is not None and nested is not value:
                values.extend(_decoded_tool_values(nested))
    elif isinstance(value, list):
        for item in value:
            values.extend(_decoded_tool_values(item))
    return values


def mailbox_sent_search_input_matches_plan(
    tool_input: object,
    plan: Mapping[str, object],
) -> bool:
    """Require a post-send search to be scoped to Sent and the exact addressee/subject."""
    if not isinstance(tool_input, Mapping):
        return False
    recipient = str(plan.get("recipient") or "").strip().casefold()
    subject = " ".join(str(plan.get("subject") or "").split()).casefold()
    query = " ".join(
        str(tool_input.get(key) or "") for key in ("query", "q", "search")
    ).casefold()
    folder_value = tool_input.get("folder", tool_input.get("mailbox", ""))
    folders = (
        [str(item).strip().casefold() for item in folder_value]
        if isinstance(folder_value, list)
        else [str(folder_value).strip().casefold()]
    )
    sent_bound = "sent" in folders or "in:sent" in query or "label:sent" in query
    recipient_bound = (
        str(tool_input.get("recipient", tool_input.get("to", ""))).strip().casefold()
        == recipient
        or recipient in query
    )
    input_subject = " ".join(str(tool_input.get("subject") or "").split()).casefold()
    subject_bound = input_subject == subject or subject in " ".join(query.split())
    return bool(recipient and subject and sent_bound and recipient_bound and subject_bound)


def mailbox_search_message_id(tool_output: object) -> str | None:
    """Extract one unambiguous provider message id from a successful search result."""
    message_ids: set[str] = set()
    for value in _decoded_tool_values(tool_output):
        if not isinstance(value, Mapping):
            continue
        raw_id = value.get(
            "provider_message_id",
            value.get("message_id", value.get("messageId", value.get("id"))),
        )
        if isinstance(raw_id, (str, int)) and str(raw_id).strip():
            message_ids.add(str(raw_id).strip())
    return next(iter(message_ids))[:180] if len(message_ids) == 1 else None


def mailbox_prepare_duplicate_receipt(
    tool_input: object,
    tool_output: object,
    plan: Mapping[str, object],
) -> dict[str, object] | None:
    """Create a body-free receipt only for an exact, structured zero-result search."""
    if not mailbox_sent_search_input_matches_plan(tool_input, plan):
        return None
    collections: list[list[object]] = []
    for value in _decoded_tool_values(tool_output):
        if not isinstance(value, Mapping):
            continue
        for key in ("messages", "results", "items", "emails"):
            result = value.get(key)
            if isinstance(result, list):
                collections.append(result)
    if not collections or any(collection for collection in collections):
        return None
    canonical = {
        "folder": "sent",
        "recipient": str(plan.get("recipient") or "").strip().casefold(),
        "subject": " ".join(str(plan.get("subject") or "").split()),
        "query": " ".join(
            str(tool_input.get(key) or "")
            for key in ("query", "q", "search")
        ).strip(),
    }
    query_digest = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "folder": "sent",
        "recipient": canonical["recipient"],
        "subject": canonical["subject"],
        "duplicate_found": False,
        "result_count": 0,
        "query_digest": query_digest,
    }


def mailbox_read_input_matches_message(tool_input: object, message_id: str) -> bool:
    """Require the read call to address the exact search-produced provider id."""
    if not isinstance(tool_input, Mapping) or not message_id:
        return False
    raw_id = tool_input.get(
        "provider_message_id",
        tool_input.get("message_id", tool_input.get("messageId", tool_input.get("id"))),
    )
    return str(raw_id or "").strip() == message_id


def _mailbox_recipient(value: object) -> str:
    if isinstance(value, list):
        return _mailbox_recipient(value[0]) if len(value) == 1 else ""
    if isinstance(value, Mapping):
        value = value.get("address", value.get("email", value.get("emailAddress", "")))
        if isinstance(value, Mapping):
            value = value.get("address", "")
    return str(value or "").strip().casefold()


def _mailbox_attachment_names(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    names: list[str] = []
    for item in value:
        raw_name = (
            item.get("name", item.get("filename"))
            if isinstance(item, Mapping)
            else item
        )
        name = Path(str(raw_name or "")).name
        if not name:
            return None
        names.append(name)
    return names


def _provider_body_sha256(message: Mapping[str, object]) -> str:
    digest = message.get("body_sha256")
    if not digest:
        provider_digest = message.get("provider_body_digest")
        if isinstance(provider_digest, Mapping):
            algorithm = str(provider_digest.get("algorithm") or "").casefold()
            digest = provider_digest.get("value") if algorithm == "sha256" else ""
        else:
            digest = provider_digest
    if not digest:
        body = message.get("body")
        if isinstance(body, str):
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return str(digest or "").strip().casefold()


def normalize_mailbox_read_receipt(
    tool_output: object,
    plan: Mapping[str, object],
    expected_message_id: str,
) -> dict[str, object] | None:
    """Derive a receipt from provider read output; never return message body content."""
    for value in _decoded_tool_values(tool_output):
        if not isinstance(value, Mapping):
            continue
        raw_folder = value.get("folder", value.get("mailbox", value.get("labels", "")))
        folders = (
            [str(item).strip().casefold() for item in raw_folder]
            if isinstance(raw_folder, list)
            else [str(raw_folder).strip().casefold()]
        )
        raw_id = value.get(
            "provider_message_id",
            value.get("message_id", value.get("messageId", value.get("id"))),
        )
        attachment_names = _mailbox_attachment_names(
            value.get("attachment_names", value.get("attachments"))
        )
        candidate = {
            "folder": "sent" if "sent" in folders else "",
            "recipient": _mailbox_recipient(
                value.get("recipient", value.get("to", value.get("recipients")))
            ),
            "subject": value.get("subject"),
            "attachment_names": attachment_names,
            "body_sha256": _provider_body_sha256(value),
            "provider_message_id": str(raw_id or "").strip(),
        }
        if candidate["provider_message_id"] != expected_message_id:
            continue
        normalized = normalize_sent_receipt(candidate, plan)
        if normalized is not None:
            return normalized
    return None


_CAPABILITY_ALIASES = {
    "mailbox_search": "mailbox_search",
    "mailbox.search": "mailbox_search",
    "email_search": "mailbox_search",
    "mailbox_get_message": "mailbox_get_message",
    "mailbox.get_message": "mailbox_get_message",
    "mailbox_read": "mailbox_get_message",
    "email_read": "mailbox_get_message",
    "direct_email_send": "direct_email_send",
    "email.send": "direct_email_send",
    "send_email": "direct_email_send",
}


@dataclass(frozen=True, slots=True)
class EmailCapabilities:
    """Semantic email abilities exposed by the current agent runtime."""

    mailbox_search: bool = False
    mailbox_get_message: bool = False
    direct_email_send: bool = False

    @classmethod
    def resolve(
        cls,
        declared: EmailCapabilities | Iterable[str] | Mapping[str, object] | None,
    ) -> EmailCapabilities:
        """Normalize common provider-neutral capability declarations.

        A mapping may use canonical capability names or aliases as boolean
        keys.  A sequence is treated as the set of available capability names.
        Unknown names are ignored so runtimes can add tools independently.
        """
        if isinstance(declared, cls):
            return declared
        if declared is None:
            return cls()

        if isinstance(declared, Mapping):
            names = [str(name) for name, enabled in declared.items() if bool(enabled)]
        elif isinstance(declared, str):
            names = [declared]
        else:
            names = [str(name) for name in declared]

        canonical = {
            resolved
            for name in names
            if (resolved := _CAPABILITY_ALIASES.get(name.strip().casefold()))
        }
        return cls(
            mailbox_search="mailbox_search" in canonical,
            mailbox_get_message="mailbox_get_message" in canonical,
            direct_email_send="direct_email_send" in canonical,
        )


@dataclass(frozen=True, slots=True)
class EmailRouteDecision:
    """Secret-free orchestration decision for one email-related step."""

    route: Literal["route_to_mailbox", "route_to_email", "requires_manual_relay"]
    action: str
    reason: str
    missing_capabilities: tuple[str, ...] = ()
    search_scope: Mapping[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        """Return a compact JSON-safe handoff envelope.

        The envelope intentionally has no field for a message body, password,
        authentication link, or verification value.
        """
        result: dict[str, object] = {
            "route": self.route,
            "action": self.action,
            "reason": self.reason,
        }
        if self.missing_capabilities:
            result["missing_capabilities"] = list(self.missing_capabilities)
        if self.search_scope is not None:
            result["search_scope"] = dict(self.search_scope)
        return result


def resolve_email_route(
    *,
    intent: EmailIntent,
    capabilities: EmailCapabilities | Iterable[str] | Mapping[str, object] | None,
    mailbox_access_authorized: bool = False,
    standing_application_authorized: bool = False,
    mailbox: str = "",
    employer: str = "",
    ats_domain: str = "",
) -> EmailRouteDecision:
    """Choose an email route from capabilities and scoped authorization.

    Verification is permitted only when the runtime can both search and read a
    shortlisted message, access is authorized, and the search can be bound to
    an exact mailbox plus the current employer or ATS.  The decision carries
    search constraints, never retrieved content or a verification value.

    A direct email application requires both a send capability and standing
    application authorization.  Otherwise the result is a resumable manual
    relay instead of a terminal browser failure.
    """
    available = EmailCapabilities.resolve(capabilities)

    if intent == "verification":
        missing = tuple(
            name
            for name, enabled in (
                ("mailbox_search", available.mailbox_search),
                ("mailbox_get_message", available.mailbox_get_message),
            )
            if not enabled
        )
        if missing:
            return EmailRouteDecision(
                route="requires_manual_relay",
                action="request_email_verification_relay",
                reason="mailbox_capability_missing",
                missing_capabilities=missing,
            )
        if not mailbox_access_authorized:
            return EmailRouteDecision(
                route="requires_manual_relay",
                action="request_mailbox_authorization",
                reason="mailbox_access_not_authorized",
            )

        normalized_mailbox = mailbox.strip().casefold()
        normalized_employer = " ".join(employer.split())
        normalized_domain = ats_domain.strip().casefold().strip(".")
        if not normalized_mailbox or not (normalized_employer or normalized_domain):
            return EmailRouteDecision(
                route="requires_manual_relay",
                action="provide_scoped_verification_context",
                reason="current_application_identity_required",
            )

        return EmailRouteDecision(
            route="route_to_mailbox",
            action="search_and_use_current_verification",
            reason="authorized_mailbox_capabilities_available",
            search_scope={
                "recipient": normalized_mailbox,
                "employer": normalized_employer,
                "ats_domain": normalized_domain,
                "max_age_minutes": 10,
                "exact_recipient": True,
                "current_application_only": True,
                "return_sensitive_value": False,
            },
        )

    if intent == "direct_application":
        if not available.direct_email_send:
            return EmailRouteDecision(
                route="requires_manual_relay",
                action="request_direct_email_send_relay",
                reason="direct_email_send_capability_missing",
                missing_capabilities=("direct_email_send",),
            )
        if not standing_application_authorized:
            return EmailRouteDecision(
                route="requires_manual_relay",
                action="request_direct_email_authorization",
                reason="direct_email_send_not_authorized",
            )
        return EmailRouteDecision(
            route="route_to_email",
            action="prepare_and_send_direct_application",
            reason="authorized_direct_email_send_available",
        )

    raise ValueError(f"unsupported email intent: {intent}")

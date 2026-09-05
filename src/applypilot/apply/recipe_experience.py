"""Persistent, value-free evidence for routine provider recipe structures.

This store is advisory only.  It never persists browser authority and a lookup
always requires a fresh caller-owned structural validation before returning a
template.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

EXPERIENCE_SCHEMA_VERSION = "provider-recipe-experience/v1"
_ALLOWED_SEMANTICS = frozenset(
    {
        "city",
        "country",
        "email",
        "phone",
        "portfolio_url",
        "preferred_name",
        "postal_code",
        "state",
    }
)
_ALLOWED_KINDS = frozenset({"text", "native_select"})
_PROVIDERS = frozenset({"greenhouse", "lever", "ashby", "smartrecruiters", "workday"})
_ADAPTER_VERSION_RE = re.compile(
    r"^(greenhouse|lever|ashby|smartrecruiters|workday)-semantic-recipe/v[1-9][0-9]*$"
)
_POLICY_VERSION_RE = re.compile(r"^routine-provider-recipe/v[1-9][0-9]*$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_OPTION_COUNT = 10_000
_MAX_EVENTS_PER_EXPERIENCE = 64
_VALIDATION_EVIDENCE = frozenset({"host_structure", "host_postcondition"})


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _event_digest(event_id: str) -> str:
    if not isinstance(event_id, str) or not event_id or len(event_id) > 512:
        raise ValueError("event_id must be a non-empty bounded string")
    return _digest({"event_id": event_id})


@dataclass(frozen=True, slots=True)
class RoutineControlTemplate:
    """One ordered routine control containing only public structure metadata."""

    semantic: str
    kind: str
    required: bool
    writable: bool
    option_count: int
    structure_signature: str | None = None

    def __post_init__(self) -> None:
        if self.semantic not in _ALLOWED_SEMANTICS:
            raise ValueError("control semantic is not routine")
        if self.kind not in _ALLOWED_KINDS:
            raise ValueError("control kind is not routine")
        if not isinstance(self.required, bool) or not isinstance(self.writable, bool):
            raise TypeError("required and writable must be booleans")
        if self.writable is not True:
            raise ValueError("routine experience controls must be writable")
        if (
            isinstance(self.option_count, bool)
            or not isinstance(self.option_count, int)
            or not 0 <= self.option_count <= _MAX_OPTION_COUNT
        ):
            raise ValueError("option_count is out of bounds")
        if self.kind == "text" and self.option_count != 0:
            raise ValueError("text controls cannot have options")
        if self.kind == "native_select" and self.option_count < 1:
            raise ValueError("native selects require options")
        if self.structure_signature is not None and not _DIGEST_RE.fullmatch(self.structure_signature):
            raise ValueError("structure_signature must be a lowercase sha256 digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "semantic": self.semantic,
            "kind": self.kind,
            "required": self.required,
            "writable": self.writable,
            "option_count": self.option_count,
            "structure_signature": self.structure_signature,
        }


@dataclass(frozen=True, slots=True)
class RecipeExperienceTemplate:
    """Content-addressed structure shared across applications and processes."""

    provider: str
    adapter_version: str
    policy_version: str
    controls: tuple[RoutineControlTemplate, ...]
    schema_version: str = EXPERIENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.provider not in _PROVIDERS:
            raise ValueError("provider is unsupported")
        version_match = _ADAPTER_VERSION_RE.fullmatch(self.adapter_version)
        if version_match is None or version_match.group(1) != self.provider:
            raise ValueError("adapter_version does not match provider")
        if _POLICY_VERSION_RE.fullmatch(self.policy_version) is None:
            raise ValueError("policy_version is unsupported")
        if self.schema_version != EXPERIENCE_SCHEMA_VERSION:
            raise ValueError("experience schema version is unsupported")
        if not isinstance(self.controls, tuple) or not 1 <= len(self.controls) <= len(_ALLOWED_SEMANTICS):
            raise ValueError("controls must be a bounded non-empty tuple")
        if not all(isinstance(control, RoutineControlTemplate) for control in self.controls):
            raise TypeError("controls must contain RoutineControlTemplate values")
        semantics = tuple(control.semantic for control in self.controls)
        if len(semantics) != len(set(semantics)):
            raise ValueError("control semantics must be unique")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "adapter_version": self.adapter_version,
            "policy_version": self.policy_version,
            "controls": [control.as_dict() for control in self.controls],
        }

    @property
    def content_key(self) -> str:
        return _digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class RecipeExperience:
    template: RecipeExperienceTemplate
    state: Literal["candidate", "validated", "invalidated"]
    observation_count: int
    validation_count: int
    invalidation_count: int


class RecipeExperienceStore:
    """Bounded SQLite store for structural evidence with sticky invalidation."""

    def __init__(self, db_path: str | Path, *, capacity: int = 128) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self._path = Path(db_path)
        self._capacity = capacity

    def observe(self, template: RecipeExperienceTemplate, *, event_id: str) -> RecipeExperience:
        return self._record(template, event_id=event_id, event_kind="observation")

    def record_validation(
        self,
        template: RecipeExperienceTemplate,
        *,
        event_id: str,
        evidence: Literal["host_structure", "host_postcondition"],
    ) -> RecipeExperience:
        if evidence not in _VALIDATION_EVIDENCE:
            raise ValueError("validation requires host structural or postcondition evidence")
        return self._record(template, event_id=event_id, event_kind=f"validated:{evidence}")

    def invalidate(self, template: RecipeExperienceTemplate, *, event_id: str) -> RecipeExperience:
        return self._record(template, event_id=event_id, event_kind="invalidated")

    def lookup(
        self,
        template: RecipeExperienceTemplate,
        *,
        adapter_version: str,
        policy_version: str,
        validate_fresh: Callable[[RecipeExperienceTemplate], bool] | None,
        tainted: bool = False,
    ) -> RecipeExperienceTemplate | None:
        """Return a validated template after a fresh check, with no authority."""

        if (
            not isinstance(template, RecipeExperienceTemplate)
            or tainted is not False
            or adapter_version != template.adapter_version
            or policy_version != template.policy_version
            or validate_fresh is None
        ):
            return None
        experience = self.get(template)
        if experience is None or experience.state != "validated":
            return None
        try:
            valid = validate_fresh(template) is True
        except Exception:  # noqa: BLE001 - missing fresh proof fails closed
            valid = False
        if not valid:
            self.invalidate(template, event_id=f"fresh-check:{time.time_ns()}")
            return None
        return experience.template

    def get(self, template: RecipeExperienceTemplate) -> RecipeExperience | None:
        if not isinstance(template, RecipeExperienceTemplate) or not self._path.is_file():
            return None
        connection = self._connect_read_only()
        try:
            row = connection.execute(
                "SELECT template_json,state,observation_count,validation_count,invalidation_count "
                "FROM recipe_experiences WHERE content_key=?",
                (template.content_key,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        finally:
            connection.close()
        if row is None:
            return None
        try:
            stored = _template_from_json(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if stored.content_key != template.content_key or stored != template:
            return None
        return RecipeExperience(stored, row[1], row[2], row[3], row[4])

    def _record(
        self,
        template: RecipeExperienceTemplate,
        *,
        event_id: str,
        event_kind: str,
    ) -> RecipeExperience:
        if not isinstance(template, RecipeExperienceTemplate):
            raise TypeError("template must be a RecipeExperienceTemplate")
        event_digest = _event_digest(event_id)
        template_json = json.dumps(template.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path, timeout=10) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_schema(connection)
            now = time.time_ns()
            connection.execute(
                "INSERT OR IGNORE INTO recipe_experiences "
                "(content_key,template_json,state,observation_count,validation_count,invalidation_count,touched_ns) "
                "VALUES(?,?,'candidate',0,0,0,?)",
                (template.content_key, template_json, now),
            )
            stored_json = connection.execute(
                "SELECT template_json FROM recipe_experiences WHERE content_key=?", (template.content_key,)
            ).fetchone()[0]
            if stored_json != template_json:
                raise ValueError("content key collision or malformed stored template")
            inserted = connection.execute(
                "INSERT OR IGNORE INTO recipe_experience_events(content_key,event_digest,event_kind) "
                "SELECT ?,?,? WHERE (SELECT COUNT(*) FROM recipe_experience_events WHERE content_key=?) < ?",
                (
                    template.content_key,
                    event_digest,
                    event_kind,
                    template.content_key,
                    _MAX_EVENTS_PER_EXPERIENCE,
                ),
            ).rowcount
            if inserted:
                if event_kind == "observation":
                    connection.execute(
                        "UPDATE recipe_experiences SET observation_count=observation_count+1,touched_ns=? "
                        "WHERE content_key=?",
                        (now, template.content_key),
                    )
                elif event_kind.startswith("validated:"):
                    connection.execute(
                        "UPDATE recipe_experiences SET validation_count=validation_count+1,"
                        "state=CASE WHEN state='invalidated' THEN state ELSE 'validated' END,touched_ns=? "
                        "WHERE content_key=?",
                        (now, template.content_key),
                    )
                else:
                    connection.execute(
                        "UPDATE recipe_experiences SET invalidation_count=invalidation_count+1,"
                        "state='invalidated',touched_ns=? WHERE content_key=?",
                        (now, template.content_key),
                    )
            elif event_kind != "observation" and connection.execute(
                "SELECT 1 FROM recipe_experience_events WHERE content_key=? AND event_digest=?",
                (template.content_key, event_digest),
            ).fetchone() is None:
                # Once the deduplication ledger is full, preserve safety state
                # transitions without allowing evidence rows or counters to grow.
                if event_kind.startswith("validated:"):
                    connection.execute(
                        "UPDATE recipe_experiences SET validation_count=CASE WHEN state='candidate' THEN "
                        "validation_count+1 ELSE validation_count END,"
                        "state=CASE WHEN state='candidate' THEN 'validated' ELSE state END,touched_ns=? "
                        "WHERE content_key=?",
                        (now, template.content_key),
                    )
                else:
                    connection.execute(
                        "UPDATE recipe_experiences SET invalidation_count=CASE WHEN state='invalidated' THEN "
                        "invalidation_count ELSE invalidation_count+1 END,state='invalidated',touched_ns=? "
                        "WHERE content_key=?",
                        (now, template.content_key),
                    )
            self._prune(connection)
        result = self.get(template)
        if result is None:
            raise RuntimeError("recipe experience write was not persisted")
        return result

    def _connect_read_only(self) -> sqlite3.Connection:
        normalized = str(self._path.resolve()).replace("\\", "/")
        return sqlite3.connect(f"file:{quote(normalized, safe='/:')}?mode=ro", uri=True, timeout=10)

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS recipe_experiences(
                content_key TEXT PRIMARY KEY,
                template_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('candidate','validated','invalidated')),
                observation_count INTEGER NOT NULL,
                validation_count INTEGER NOT NULL,
                invalidation_count INTEGER NOT NULL,
                touched_ns INTEGER NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS recipe_experience_events(
                content_key TEXT NOT NULL REFERENCES recipe_experiences(content_key) ON DELETE CASCADE,
                event_digest TEXT NOT NULL,
                event_kind TEXT NOT NULL,
                PRIMARY KEY(content_key,event_digest)
            )"""
        )

    def _prune(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "DELETE FROM recipe_experiences WHERE content_key IN ("
            "SELECT content_key FROM recipe_experiences ORDER BY touched_ns DESC,content_key DESC LIMIT -1 OFFSET ?)",
            (self._capacity,),
        )


def _template_from_json(raw: str) -> RecipeExperienceTemplate:
    payload = json.loads(raw)
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "provider",
        "adapter_version",
        "policy_version",
        "controls",
    }:
        raise ValueError("stored template has an invalid shape")
    controls = payload["controls"]
    if not isinstance(controls, list):
        raise TypeError("stored controls must be a list")
    parsed_controls = []
    for control in controls:
        if not isinstance(control, dict) or set(control) != {
            "semantic",
            "kind",
            "required",
            "writable",
            "option_count",
            "structure_signature",
        }:
            raise ValueError("stored control has an invalid shape")
        parsed_controls.append(RoutineControlTemplate(**control))
    return RecipeExperienceTemplate(
        provider=payload["provider"],
        adapter_version=payload["adapter_version"],
        policy_version=payload["policy_version"],
        controls=tuple(parsed_controls),
        schema_version=payload["schema_version"],
    )


__all__ = [
    "EXPERIENCE_SCHEMA_VERSION",
    "RecipeExperience",
    "RecipeExperienceStore",
    "RecipeExperienceTemplate",
    "RoutineControlTemplate",
]

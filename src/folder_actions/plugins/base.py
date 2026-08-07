# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""The plug-in contract for folder actions (SPECIFICATIONS.md §6 / §6.1).

An action is an in-process plug-in that reacts to a recognized event on a file in
a bound folder. Each plug-in:
  - declares a ``type_name`` / ``label`` / ``supported_events``,
  - validates its ``binding.config`` with a pydantic ``ConfigModel``,
  - publishes ``config_fields()`` — a typed FieldDescriptor list so a generic
    frontend renders its form with no plug-in-specific code (§6.1),
  - runs ``execute(event, config, ctx)`` and returns an ``ActionResult``.

Built-ins register via the ``register`` decorator; third-party plug-ins register
through the ``folder_actions.actions`` setuptools entry-point group. ``ActionContext``
is the plug-in's only capability surface — every core mutation goes through the
service-principal client on it (§7.5), which also drives loop-avoidance (§3.3).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # avoid import cycles; these are duck-typed at runtime
    from ..config import Config
    from ..core_client import CoreClient
    from ..csai_client import CsaiClient
    from ..directory import Directory
    from ..discussion_client import DiscussionClient
    from ..mailer import SmtpMailer
    from ..mime import MimeResolver
    from ..secrets import SecretBox
    from ..stores import Store

log = logging.getLogger("folder_actions.plugins")

# ---------------------------------------------------------------------------
# Field descriptors — the generic form contract (§6.1)
# ---------------------------------------------------------------------------

# The standard field-type catalog the frontend widget registry understands.
FIELD_TYPES = (
    "string", "text", "integer", "number", "boolean", "select", "multiselect",
    "secret", "folder", "file", "principal", "ref", "group",
)


class FieldOption(BaseModel):
    value: str
    label: str


class VisibleWhen(BaseModel):
    key: str
    equals: Any


class FieldDescriptor(BaseModel):
    """A single config field a plug-in publishes for the generic form renderer.

    Only the attributes relevant to ``type`` need be set; the frontend maps ``type``
    to a widget and honours the constraints. The server also validates against these
    (plus the plug-in's ConfigModel) on write — a generic UI never weakens
    server-side validation (§6.1)."""
    key: str
    label: str
    type: str = Field(..., description="one of FIELD_TYPES")
    required: bool = False
    default: Any = None
    help: Optional[str] = None
    # number / integer
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    # string
    max_length: Optional[int] = None
    pattern: Optional[str] = None
    # select / multiselect
    options: Optional[list[FieldOption]] = None
    options_source: Optional[str] = None  # event_catalog | classifier_sets | mime_catalog
    # group (repeatable rows)
    item_fields: Optional[list["FieldDescriptor"]] = None
    # secret (write-only) / conditional visibility
    secret: bool = False
    visible_when: Optional[VisibleWhen] = None

    def model_post_init(self, __context: Any) -> None:
        if self.type not in FIELD_TYPES:
            raise ValueError(f"unknown field type {self.type!r}; expected one of {FIELD_TYPES}")


FieldDescriptor.model_rebuild()


# ---------------------------------------------------------------------------
# Execution result + context
# ---------------------------------------------------------------------------

# Terminal outcomes recorded in action_run (§8/§10).
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


@dataclass
class ActionResult:
    """What a plug-in did for one event. ``retryable`` lets the consumer redeliver
    (leave the entry un-acked) on a transient failure; a terminal failure is acked."""
    status: str = STATUS_DONE
    detail: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False

    @classmethod
    def done(cls, **detail: Any) -> "ActionResult":
        return cls(STATUS_DONE, detail)

    @classmethod
    def skipped(cls, reason: str, **detail: Any) -> "ActionResult":
        return cls(STATUS_SKIPPED, {"reason": reason, **detail})

    @classmethod
    def failed(cls, reason: str, *, retryable: bool = False, **detail: Any) -> "ActionResult":
        return cls(STATUS_FAILED, {"reason": reason, **detail}, retryable=retryable)


@dataclass
class ActionContext:
    """The plug-in's capability surface. All core mutations use ``core`` (the
    folder_actions service principal, §7.5). Handles are duck-typed to avoid import
    cycles; see the referenced modules for their APIs."""
    tenant: str
    binding_id: str
    folder_uid: str
    core: "CoreClient"
    csai: "CsaiClient"
    directory: "Directory"
    discussion: "DiscussionClient"
    mailer: "SmtpMailer"
    secrets: "SecretBox"
    mime: "MimeResolver"
    store: "Store"
    config: "Config"
    log: logging.Logger = log


# ---------------------------------------------------------------------------
# Plug-in protocol + registry
# ---------------------------------------------------------------------------

@runtime_checkable
class ActionPlugin(Protocol):
    type_name: ClassVar[str]
    label: ClassVar[str]
    supported_events: ClassVar[frozenset[str]]
    ConfigModel: ClassVar[type[BaseModel]]

    # Loop-safety flag (§3.3). Set True for actions that move (or otherwise re-emit
    # events for) files **unattended** — i.e. with no human gate between the trigger
    # and the mutation (the sorter is the canonical example). The consumer then
    # short-circuits such an action on `file.moved` events actored by the service
    # principal, so it can never cascade on folder_actions' own moves. Actions whose
    # mutations are human-gated (e.g. raise_review → a person approves → move) leave
    # this False so they can participate in chains. Defaults to False when absent.
    auto_moves: ClassVar[bool] = False

    @classmethod
    def config_fields(cls) -> list[FieldDescriptor]: ...

    def execute(self, event: dict, config: BaseModel, ctx: ActionContext) -> ActionResult: ...


_REGISTRY: dict[str, type] = {}


def register(cls: type) -> type:
    """Decorator: register a built-in ActionPlugin by its ``type_name``."""
    name = getattr(cls, "type_name", None)
    if not name:
        raise ValueError(f"{cls!r} has no type_name")
    if name in _REGISTRY:
        raise ValueError(f"duplicate action type_name {name!r}")
    _REGISTRY[name] = cls
    return cls


def load_entrypoint_plugins() -> None:
    """Discover third-party plug-ins from the ``folder_actions.actions`` group."""
    try:
        eps = importlib_metadata.entry_points(group="folder_actions.actions")
    except Exception:  # pragma: no cover - importlib API drift
        return
    for ep in eps:
        try:
            cls = ep.load()
            register(cls)
        except Exception:
            log.warning("failed to load action plug-in %s", ep.name, exc_info=True)


def get_plugin(type_name: str) -> Optional[type]:
    return _REGISTRY.get(type_name)


def registry(enabled: Optional[set[str]] = None) -> dict[str, type]:
    """The active plug-ins, optionally restricted by an allowlist (FA_ENABLED_ACTIONS)."""
    if enabled is None:
        return dict(_REGISTRY)
    return {k: v for k, v in _REGISTRY.items() if k in enabled}

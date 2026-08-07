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

"""Event -> binding resolution and folder scoping (SPECIFICATIONS.md §3.2).

Pure, side-effect-free matching (the only outward calls are ``core`` reads, used to
resolve a file's current parent for anchored events and to walk ancestry for
``recursive`` bindings). Path strings in the envelope are advisory and never used
for a routing decision — membership is always tested against live ``parent_uid``
ancestry via the core client."""
from __future__ import annotations

import logging
from typing import Iterable

log = logging.getLogger("folder_actions.matching")

# Events anchored to a ``file_uid`` with no reliable ``parent_uid`` in the envelope —
# folder membership is resolved from that file's *current* parent via core (§3.2).
# Review/comment events are file-anchored by nature; ``conversion.*`` events are
# emitted by CSAI (not the core publisher) and are NOT enriched with parent_uid, so
# they must be resolved the same way.
_ANCHORED_PREFIXES = ("review.", "thread.", "comment.", "mention.", "conversion.")

# Bound depth for the recursive-binding ancestry walk (§3.2 "bounded depth, cached").
_MAX_ANCESTRY_DEPTH = 32


def _is_anchored_event(event_type: str) -> bool:
    return any(event_type.startswith(p) for p in _ANCHORED_PREFIXES)


def folder_uids_for_event(event: dict, core) -> set[str]:
    """The candidate folder uid(s) an event pertains to (non-empty only).

    - File/dir events: the entity's immediate ``parent_uid`` (it lives in that
      folder) plus its own ``file_uid`` (an event *about* the folder itself).
    - Review/comment events (``review.*`` / ``thread.*`` / ``comment.*`` /
      ``mention.*``): anchored to a ``file_uid`` — resolve that file's current
      parent via ``core.parent_of`` (exceptions are guarded and yield nothing).
    """
    event_type = event.get("type") or ""
    uids: set[str] = set()

    if _is_anchored_event(event_type):
        file_uid = event.get("file_uid")
        if file_uid:
            try:
                parent = core.parent_of(file_uid)
            except Exception:
                parent = ""
            if parent:
                uids.add(parent)
        return uids

    parent_uid = event.get("parent_uid")
    file_uid = event.get("file_uid")
    if parent_uid:
        uids.add(parent_uid)
    if file_uid:
        uids.add(file_uid)
        # Fallback: an emitter that didn't enrich parent_uid — resolve the current parent.
        if not parent_uid:
            try:
                parent = core.parent_of(file_uid)
            except Exception:
                parent = ""
            if parent:
                uids.add(parent)
    return uids


def _is_ancestor(ancestor_uid: str, start_uids: Iterable[str], core,
                 max_depth: int = _MAX_ANCESTRY_DEPTH) -> bool:
    """True if ``ancestor_uid`` sits above any of ``start_uids`` in the folder tree.

    Walks ``parent_of`` upward from each start uid (bounded depth), caching lookups
    for the duration of the call and guarding cycles / broken links / core errors."""
    cache: dict[str, str] = {}

    def parent_of(uid: str) -> str:
        if uid not in cache:
            try:
                cache[uid] = core.parent_of(uid) or ""
            except Exception:
                cache[uid] = ""
        return cache[uid]

    for start in start_uids:
        current = start
        seen: set[str] = set()
        depth = 0
        while current and current not in seen and depth < max_depth:
            seen.add(current)
            parent = parent_of(current)
            if not parent:
                break
            if parent == ancestor_uid:
                return True
            current = parent
            depth += 1
    return False


def binding_matches(binding: dict, event: dict, core) -> bool:
    """True if ``binding`` should fire for ``event``.

    A binding fires when it is enabled, the event ``type`` is one it listens for
    (``on_events``), and its folder is in scope: the bound folder is a candidate
    folder of the event, or — for a ``recursive`` binding — an ancestor of one."""
    if not binding.get("enabled", True):
        return False

    event_type = event.get("type") or ""
    on_events = binding.get("on_events") or []
    if event_type not in on_events:
        return False

    folder_uid = binding.get("folder_uid")
    if not folder_uid:
        return False

    candidates = folder_uids_for_event(event, core)
    if not candidates:
        return False
    if folder_uid in candidates:
        return True

    if binding.get("recursive"):
        return _is_ancestor(folder_uid, candidates, core)
    return False


def bindings_for_event(event: dict, store, core) -> list[dict]:
    """All enabled bindings (for the event's tenant) that match ``event`` (§3.2)."""
    tenant = event.get("tenant") or "default"
    bindings = store.list_enabled_bindings(tenant)
    return [b for b in bindings if binding_matches(b, event, core)]

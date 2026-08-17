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

"""Periodic reconcile sweep (SPECIFICATIONS.md §8).

The event stream is at-least-once and *fail-open*: the core publisher drops-oldest
on Redis backpressure and retention is capped, so events can be missed during a
Redis outage or beyond stream retention. This sweep recovers that work by walking
the folders that actually have bindings, listing the files that changed inside the
sweep window, and re-driving them through the *same* dispatch path the live
consumer uses. It mirrors the reconcile sweeps in ``convert_search_ai`` /
``discussion``, but scoped to bound folders rather than the whole tree.

**It reconstructs state, not transitions.** A file sitting in a bound folder with a
version is evidence that it was created/updated there; nothing in core tells you
retrospectively that a *move* happened, that a review was approved, or that a
comment was posted. So only the event types in ``RECONCILABLE_EVENTS`` are
synthesized — a binding listening solely for, say, ``review.approved`` is outside
what a sweep can recover and is skipped (it is logged in the per-sweep counters as
``bindings_unreconcilable``). Recovering those would need the discussion service's
own state, which is its reconcile's job, not this one's.

**Idempotency.** Two layers. Synthesized event ids are deterministic, so repeat
sweeps collapse onto one ``action_run`` primary key. And dispatch runs with
``collapse_on_content=True``, which additionally skips any binding that already
recorded a run for the file at the same version *or* at any version after the file
last changed — so work the live consumer already did under the core's own event id
is not repeated. That second clause matters more than it looks: a core
``file.created`` event carries an empty version, so a version-equality check alone
lets every newly-created file run twice. A retryable outcome records no run,
leaving it eligible for the next sweep.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone

from .config import Config, load_dotenv
from .consumer import EventConsumer
from .core_client import CoreClient
from .plugins.base import load_entrypoint_plugins
from .stores import Store

log = logging.getLogger("folder_actions.reconcile")

# Event types a sweep can honestly reconstruct from core state alone (see module
# docstring). ``conversion.complete`` is included because the sorter's inbox flow
# depends on it and the plug-in itself verifies the extracted text is actually
# ready — a synthesized one for a file CSAI has not converted yet comes back
# retryable and is simply retried next sweep, never recorded as done.
RECONCILABLE_EVENTS = ("file.created", "file.updated", "conversion.complete")


def _event_id(event_type: str, file_uid: str, version: str) -> str:
    """A stable synthetic event id for one (type, file, version).

    Deterministic so repeated sweeps land on the same ``action_run`` primary key
    instead of accumulating a row per sweep. Namespaced ``reconcile:`` so the log
    plainly distinguishes recovered work from live-stream work."""
    digest = hashlib.sha1(
        f"{event_type}|{file_uid}|{version}".encode("utf-8")).hexdigest()[:16]
    return f"reconcile:{event_type}:{digest}"


def _as_aware(dt):
    """Treat a naive timestamp as UTC (core/psycopg may hand back either)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class Reconciler:
    """Walks bound folders and re-drives recently-changed files through the consumer.

    ``consumer`` and ``core_factory`` are injectable for tests; by default a single
    ``EventConsumer`` is shared across the sweep (its per-tenant core clients and
    capability clients are already cached internally)."""

    def __init__(self, config: Config, store: Store, *, consumer=None,
                 core_factory=None) -> None:
        self.config = config
        self.store = store
        self.consumer = consumer or EventConsumer(config, store)
        self._core_factory = core_factory or (lambda tenant: CoreClient(config, tenant))
        self._core_cache: dict[str, CoreClient] = {}

    def _core(self, tenant: str) -> CoreClient:
        if tenant not in self._core_cache:
            self._core_cache[tenant] = self._core_factory(tenant)
        return self._core_cache[tenant]

    # ------------------------------------------------------------------- window
    def window_start(self, tenant: str, now: datetime) -> datetime:
        """Opening edge of this sweep's change window.

        Normally the previous sweep's watermark minus an overlap (so a file written
        while that sweep was mid-walk is not skipped by both). Clamped to
        ``reconcile_lookback_s`` so a first sweep — or one after a very long outage —
        cannot walk unbounded history in a single pass; successive sweeps then step
        the watermark forward."""
        floor = now - timedelta(seconds=self.config.reconcile_lookback_s)
        try:
            mark = _as_aware(self.store.get_reconcile_watermark(tenant))
        except Exception:
            log.warning("reconcile: watermark read failed for %s; using full lookback",
                        tenant, exc_info=True)
            return floor
        if mark is None:
            return floor
        start = mark - timedelta(seconds=self.config.reconcile_overlap_s)
        return max(start, floor)

    # -------------------------------------------------------------- enumeration
    def _folders_to_walk(self, bindings: list[dict]) -> dict[str, bool]:
        """Bound folder uid -> whether any binding on it is recursive.

        Several bindings commonly share one folder; listing it once and letting the
        matcher fan back out to every binding keeps the core reads proportional to
        folders, not bindings."""
        folders: dict[str, bool] = {}
        for b in bindings:
            uid = b.get("folder_uid")
            if not uid:
                continue
            folders[uid] = folders.get(uid, False) or bool(b.get("recursive"))
        return folders

    def _changed_files(self, core: CoreClient, folder_uid: str, recursive: bool,
                       since: datetime, budget: list) -> list[dict]:
        """Files under ``folder_uid`` modified at/after ``since``.

        Iterative, depth-bounded walk; ``budget`` is a one-element list holding the
        remaining file allowance, decremented as entries are *examined* so a huge
        tree cannot starve later folders. A folder that vanishes or is unreadable
        mid-walk is skipped, never fatal (it is a live filesystem)."""
        out: list[dict] = []
        stack = [(folder_uid, 0)]
        seen: set[str] = set()

        while stack:
            uid, depth = stack.pop()
            if uid in seen or budget[0] <= 0:
                continue
            seen.add(uid)
            try:
                entries = core.listdir(uid)
            except Exception:
                log.debug("reconcile: could not list %s; skipping", uid, exc_info=True)
                continue

            for e in entries:
                if budget[0] <= 0:
                    break
                if getattr(e, "is_container", False):
                    if recursive and depth + 1 < self.config.reconcile_max_depth:
                        stack.append((e.uid, depth + 1))
                    continue
                budget[0] -= 1
                modified = _as_aware(getattr(e, "modified_at", None))
                # No timestamp => can't place it in the window; skip rather than
                # re-run every action on it on every single sweep.
                if modified is None or modified < since:
                    continue
                out.append({
                    "uid": e.uid,
                    "name": getattr(e, "name", "") or "",
                    "parent_uid": uid,
                    "modified_by": getattr(e, "modified_by", "") or "",
                    "modified_at": modified,
                })
        return out

    # ---------------------------------------------------------------- dispatch
    def _event_types_for(self, bindings: list[dict]) -> tuple[set[str], int]:
        """The reconcilable event types these bindings listen for, and a count of
        bindings that listen for nothing reconcilable (reported, not silently lost)."""
        types: set[str] = set()
        unreconcilable = 0
        for b in bindings:
            on_events = set(b.get("on_events") or [])
            hits = on_events.intersection(RECONCILABLE_EVENTS)
            if hits:
                types |= hits
            elif on_events:
                unreconcilable += 1
        return types, unreconcilable

    def _version_of(self, core: CoreClient, file_uid: str) -> str:
        """The file's current version string — the content-collapse key (§8)."""
        try:
            return getattr(core.stat(file_uid), "version", "") or ""
        except Exception:
            log.debug("reconcile: stat failed for %s", file_uid, exc_info=True)
            return ""

    def sweep_tenant(self, tenant: str, now: datetime | None = None) -> dict:
        """Sweep one tenant. Returns counters; never raises for per-item failures."""
        now = now or datetime.now(timezone.utc)
        counts = {"folders": 0, "candidates": 0, "dispatched": 0, "errors": 0,
                  "bindings_unreconcilable": 0, "truncated": 0}

        bindings = self.store.list_enabled_bindings(tenant)
        if not bindings:
            # Still advance the watermark: there was nothing to miss.
            self._advance(tenant, now)
            return counts

        types, counts["bindings_unreconcilable"] = self._event_types_for(bindings)
        if not types:
            self._advance(tenant, now)
            return counts

        since = self.window_start(tenant, now)
        core = self._core(tenant)
        budget = [max(0, int(self.config.reconcile_max_files))]

        for folder_uid, recursive in self._folders_to_walk(bindings).items():
            if budget[0] <= 0:
                counts["truncated"] += 1
                continue
            counts["folders"] += 1
            for f in self._changed_files(core, folder_uid, recursive, since, budget):
                counts["candidates"] += 1
                version = self._version_of(core, f["uid"])
                for event_type in sorted(types):
                    if self._dispatch(tenant, event_type, f, version):
                        counts["dispatched"] += 1
                    else:
                        counts["errors"] += 1

        if budget[0] <= 0:
            counts["truncated"] += 1
            log.warning(
                "reconcile(%s): hit FA_RECONCILE_MAX_FILES=%s — the window was NOT "
                "fully covered; the watermark is left in place so the next sweep "
                "resumes from the same point", tenant, self.config.reconcile_max_files)
        else:
            self._advance(tenant, now)

        log.info("reconcile(%s): %s (window from %s)", tenant, counts, since.isoformat())
        return counts

    def _dispatch(self, tenant: str, event_type: str, f: dict, version: str) -> bool:
        """Feed one synthesized event through the live dispatch path. ``False`` on an
        unexpected failure (already logged) — matching/plug-in outcomes are not errors."""
        event = {
            "type": event_type,
            "event_id": _event_id(event_type, f["uid"], version),
            "tenant": tenant,
            "file_uid": f["uid"],
            "parent_uid": f["parent_uid"],
            "name": f["name"],
            "version": version,
            # The last reviser is the closest thing to the original actor. It must
            # NOT be the service principal, or the sorter's loop guard would treat
            # recovered work as self-generated — but that guard only inspects
            # file.moved, which is never synthesized.
            "actor": f["modified_by"],
            "reconciled": True,
            # The idempotency guard's time bound: any run this binding already
            # recorded for the file AFTER it last changed means the live consumer
            # (or an earlier sweep) got there first. Carried on the event so the
            # shared dispatch path needs no extra parameter.
            "reconciled_since": f["modified_at"],
        }
        try:
            self.consumer.handle(event, collapse_on_content=True)
            return True
        except Exception:
            log.exception("reconcile: dispatch failed for %s (%s)", f["uid"], event_type)
            return False

    def _advance(self, tenant: str, when: datetime) -> None:
        try:
            self.store.set_reconcile_watermark(tenant, when)
        except Exception:
            # A watermark that fails to advance only means the next sweep re-covers
            # the same window — idempotent, so it is safe to carry on.
            log.warning("reconcile: could not advance watermark for %s", tenant,
                        exc_info=True)

    # ------------------------------------------------------------------- sweep
    def sweep_once(self) -> dict:
        """Sweep every provisioned tenant. One tenant failing never stops the rest."""
        totals: dict[str, int] = {}
        try:
            tenants = self.store.list_tenants()
        except Exception:
            log.exception("reconcile: could not enumerate tenants; skipping sweep")
            return totals
        for tenant in tenants:
            try:
                for k, v in self.sweep_tenant(tenant).items():
                    totals[k] = totals.get(k, 0) + v
            except Exception:
                log.exception("reconcile: sweep failed for tenant %s", tenant)
        return totals


def reconcile_once(config: Config, store: Store, core=None) -> dict:
    """Run a single reconcile sweep across all tenants.

    ``core`` is accepted for backward compatibility with the previous stub signature
    and is ignored — the sweep builds a core client per tenant, since bindings and
    files are tenant-scoped."""
    return Reconciler(config, store).sweep_once()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    config = Config()
    if not config.reconcile_enabled:
        log.info("folder_actions reconcile disabled (FA_RECONCILE_ENABLED=false)")
        return
    store = Store(config)
    # Third-party action plug-ins must be registered here too — the sweep dispatches
    # through the same registry the consumer uses (built-ins register on import).
    load_entrypoint_plugins()
    reconciler = Reconciler(config, store)
    log.info("folder_actions reconcile started (interval=%ss lookback=%ss max_files=%s)",
             config.reconcile_interval_s, config.reconcile_lookback_s,
             config.reconcile_max_files)
    try:
        while True:
            try:
                reconciler.sweep_once()
            except Exception:
                log.exception("reconcile sweep failed")
            time.sleep(config.reconcile_interval_s)
    except KeyboardInterrupt:  # pragma: no cover - operator stop
        log.info("folder_actions reconcile stopping")


if __name__ == "__main__":  # pragma: no cover
    main()

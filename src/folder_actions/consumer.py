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

"""The recognized-event worker (SPECIFICATIONS.md §3, §8).

Reads the shared ``fileengine:events`` stream through the ``RedisEventSource``,
resolves each event to its matching folder bindings (``matching.py``), and runs
each binding's action plug-in as the folder_actions service principal. Execution is
idempotent per ``(event_id, binding_id)`` (the ``action_run`` unique key), bindings
are isolated (one failing never aborts the loop), and entries are acked at-least-once
only after they reach a terminal state — a plug-in that returns a *retryable*
``ActionResult`` leaves its entry un-acked for redelivery (§8)."""
from __future__ import annotations

import logging

from pydantic import ValidationError

from .config import Config, load_dotenv
from .core_client import CoreClient, service_actor
from .csai_client import CsaiClient
from .directory import Directory
from .events import RedisEventSource
from .mailer import SmtpMailer
from .matching import bindings_for_event
from .mime import MimeResolver
from .plugins.base import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_SKIPPED,
    ActionContext,
    ActionResult,
    load_entrypoint_plugins,
    registry,
)
from .secrets import SecretBox
from .stores import Store

log = logging.getLogger("folder_actions.consumer")


class EventConsumer:
    """Consume recognized events and dispatch matching bindings to their plug-ins.

    Shared capability clients (core, csai, directory, mailer, secrets, mime) are
    constructed once and reused across every binding on every event; they can be
    injected (for tests) or are built from ``config`` by default."""

    def __init__(self, config: Config, store: Store, *, core=None, csai=None,
                 directory=None, mailer=None, secrets=None, mime=None) -> None:
        self.config = config
        self.store = store
        self.core = core or CoreClient(config)
        self.csai = csai or CsaiClient(config)
        self.directory = directory or Directory(config)
        self.mailer = mailer or SmtpMailer(config)
        self.secrets = secrets or SecretBox(config.secret_key)
        self.mime = mime or MimeResolver(self.core)
        self.service_actor = service_actor(config)

    # ------------------------------------------------------------------ dispatch
    def handle(self, event: dict) -> bool:
        """Process one event across all its matching bindings.

        Returns ``True`` if the entry should be left **un-acked** for redelivery
        (any binding produced a retryable, non-terminal result); ``False`` when the
        event is fully resolved and may be acked."""
        event_type = event.get("type") or ""
        event_id = event.get("event_id") or ""

        # (1) CSAI's own rendition writes are ignored (avoid feedback, §3.1).
        if event.get("is_rendition"):
            log.debug("ignoring rendition event %s (%s)", event_id, event_type)
            return False

        # (2) Loop avoidance: ignore our own service-principal moves (§3.3).
        if event_type == "file.moved" and event.get("actor") == self.service_actor:
            log.debug("ignoring self-generated move %s by service principal", event_id)
            return False

        tenant = event.get("tenant") or "default"
        try:
            bindings = bindings_for_event(event, self.store, self.core)
        except Exception:
            log.exception("failed to resolve bindings for event %s", event_id)
            return False

        redeliver = False
        for binding in bindings:
            try:
                if self._run_binding(event, binding, tenant, event_id):
                    redeliver = True
            except Exception:
                # Isolation: one binding failing never aborts the loop (§6/§8).
                log.exception("binding %s failed on event %s",
                              binding.get("id"), event_id)
                self._record_failed(tenant, event, binding, event_id,
                                    {"reason": "exception"})
        return redeliver

    def _run_binding(self, event: dict, binding: dict, tenant: str,
                     event_id: str) -> bool:
        """Run a single binding for an event. Returns ``True`` iff the run was a
        retryable (non-terminal) failure and the entry should not yet be acked."""
        binding_id = str(binding.get("id"))
        action_type = binding.get("action_type") or ""
        file_uid = event.get("file_uid") or ""
        version = event.get("version") or ""

        # Dedupe: a completed (event_id, binding_id) run is a no-op (§8).
        if self.store.run_exists(tenant, event_id, binding_id):
            log.debug("binding %s already ran for event %s (dedupe)", binding_id, event_id)
            return False

        plugin_cls = registry(self.config.enabled_actions or None).get(action_type)
        if plugin_cls is None:
            log.warning("unknown/disabled action_type %r for binding %s; skipping",
                        action_type, binding_id)
            self.store.record_run(
                tenant, event_id=event_id, binding_id=binding_id,
                action_type=action_type, file_uid=file_uid, version=version,
                status=STATUS_SKIPPED,
                detail={"reason": "unknown_action_type", "action_type": action_type})
            return False

        # Server-side config validation stays authoritative (§6.1).
        try:
            cfg_model = plugin_cls.ConfigModel.model_validate(binding.get("config") or {})
        except ValidationError as e:
            log.warning("invalid config for binding %s (%s): %s",
                        binding_id, action_type, e)
            self.store.record_run(
                tenant, event_id=event_id, binding_id=binding_id,
                action_type=action_type, file_uid=file_uid, version=version,
                status=STATUS_FAILED,
                detail={"reason": "invalid_config", "errors": e.errors()})
            return False

        ctx = ActionContext(
            tenant=tenant, binding_id=binding_id,
            folder_uid=binding.get("folder_uid") or "",
            core=self.core, csai=self.csai, directory=self.directory,
            mailer=self.mailer, secrets=self.secrets, mime=self.mime,
            store=self.store, config=self.config, log=log)

        try:
            result = plugin_cls().execute(event, cfg_model, ctx)
        except Exception:
            log.exception("action %s raised on binding %s / event %s",
                          action_type, binding_id, event_id)
            self.store.record_run(
                tenant, event_id=event_id, binding_id=binding_id,
                action_type=action_type, file_uid=file_uid, version=version,
                status=STATUS_FAILED, detail={"reason": "exception"})
            return False

        if not isinstance(result, ActionResult):  # tolerate a misbehaving plug-in
            result = ActionResult()

        # A retryable failure is *not* terminal: leave the entry un-acked and do not
        # record a run (so redelivery re-attempts rather than dedupe-skipping) (§8).
        if result.retryable and result.status != STATUS_DONE:
            log.info("binding %s: retryable failure on event %s — leaving un-acked",
                     binding_id, event_id)
            return True

        self.store.record_run(
            tenant, event_id=event_id, binding_id=binding_id,
            action_type=action_type, file_uid=file_uid, version=version,
            status=result.status, detail=result.detail or {})
        return False

    def _record_failed(self, tenant: str, event: dict, binding: dict,
                       event_id: str, detail: dict) -> None:
        try:
            self.store.record_run(
                tenant, event_id=event_id, binding_id=str(binding.get("id")),
                action_type=binding.get("action_type") or "",
                file_uid=event.get("file_uid") or "",
                version=event.get("version") or "",
                status=STATUS_FAILED, detail=detail)
        except Exception:
            log.exception("failed to record run for binding %s / event %s",
                          binding.get("id"), event_id)

    # --------------------------------------------------------------------- loop
    def run_forever(self, source: RedisEventSource) -> None:
        """Poll the stream and process batches until interrupted (§8: ack after a
        terminal state; a retryable entry is left un-acked for redelivery)."""
        source.ensure_group()
        log.info("folder_actions consumer started (stream=%s group=%s consumer=%s)",
                 source.stream, source.group, source.consumer)
        try:
            while True:
                for msg_id, event in source.read(count=32, block_ms=5000):
                    redeliver = False
                    try:
                        redeliver = self.handle(event)
                    except Exception:
                        # Poison / unexpected error: log, count, and ack (§8).
                        log.exception("unhandled error processing entry %s", msg_id)
                        redeliver = False
                    if not redeliver:
                        source.ack([msg_id])
        except KeyboardInterrupt:  # pragma: no cover - operator stop
            log.info("folder_actions consumer stopping")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    config = Config()
    store = Store(config)
    # Register any third-party action plug-ins (built-ins register on import).
    load_entrypoint_plugins()
    consumer = EventConsumer(config, store)
    source = RedisEventSource(config, config.consumer_name)
    consumer.run_forever(source)


if __name__ == "__main__":  # pragma: no cover
    main()

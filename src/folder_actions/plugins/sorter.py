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

"""Automatic sorter action (SPECIFICATIONS.md §7.3).

Classifies a file's extracted Markdown against a bound classifier set and routes it
to a destination folder per a per-binding routing table. Fires on
``conversion.complete`` (new content) and ``file.moved`` (an existing file dropped
into the inbox). Winner selection is highest-score-then-priority; no-match leaves
the file in place; already-in-destination is a no-op."""
from __future__ import annotations

import logging

from pydantic import BaseModel

from ..classifier import document_classifier_simple
from ..csai_client import TextNotReady
from .base import ActionContext, ActionResult, FieldDescriptor, register

log = logging.getLogger("folder_actions.plugins.sorter")


@register
class SorterAction:
    type_name = "sorter"
    label = "Automatic sorter"
    supported_events = frozenset({"conversion.complete", "file.moved"})
    # Moves files unattended (no human gate) -> must not re-fire on its own moves (§3.3).
    auto_moves = True

    class ConfigModel(BaseModel):
        # The reusable classifier set to score against; the per-folder routing table
        # (threshold + destination + priority per classification) lives in the Store
        # (sorter_route) and is edited through a separate API, not this config.
        classifier_set_id: str

    @classmethod
    def config_fields(cls) -> list[FieldDescriptor]:
        return [
            FieldDescriptor(
                key="classifier_set_id", label="Classifier set", type="ref",
                required=True, options_source="classifier_sets",
                help="The classifier set to score documents against. The routing "
                     "table (classification -> threshold + destination + priority) "
                     "is edited separately in the sorter routing editor.",
            ),
        ]

    def execute(self, event: dict, config: "SorterAction.ConfigModel",
                ctx: ActionContext) -> ActionResult:
        etype = event.get("type")
        file_uid = event.get("file_uid")
        if not file_uid:
            return ActionResult.skipped("no_file_uid")

        # (1) Extracted Markdown from CSAI. On file.moved the file may not be
        # converted yet -> defer by requesting conversion and let the ensuing
        # conversion.complete re-fire the sort. On conversion.complete the text is
        # guaranteed present, so TextNotReady there is a transient anomaly -> retry.
        try:
            text = ctx.csai.get_text(file_uid, ctx.tenant)
        except TextNotReady:
            if etype == "file.moved":
                ctx.csai.request_convert(file_uid, ctx.tenant)
                return ActionResult.skipped("deferred_conversion", file_uid=file_uid)
            return ActionResult.failed("text_not_ready", retryable=True, file_uid=file_uid)
        except Exception:
            ctx.log.warning("sorter: text fetch failed for %s", file_uid, exc_info=True)
            return ActionResult.failed("text_fetch_error", retryable=True, file_uid=file_uid)

        # (2) Load the classifier set and score (unbounded weighted sums, §7.3).
        cset = ctx.store.get_classifier_set_full(ctx.tenant, config.classifier_set_id)
        if not cset:
            return ActionResult.failed("classifier_set_missing",
                                       classifier_set_id=config.classifier_set_id)
        classifications = [
            {"name": c["name"],
             "terms": [{"term": t["term"], "distance": t["distance"], "weight": t["weight"]}
                       for t in c.get("terms", [])]}
            for c in cset.get("classifiers", [])
        ]
        if not classifications:
            return ActionResult.skipped("empty_classifier_set")
        scores = document_classifier_simple(text, classifications)

        # (3) Routing table for this binding, keyed by classification name.
        routes = ctx.store.get_routes(ctx.tenant, ctx.binding_id)
        route_by_name = {r["classification_name"]: r for r in routes}

        # Winners: score >= threshold. scores iterates in declared order, so a stable
        # sort preserves first-declared as the final tie fallback (§7.3.4).
        winners = []
        for name, score in scores.items():
            route = route_by_name.get(name)
            if route is None:
                continue
            if score >= float(route.get("threshold", 0)):
                winners.append((name, score, route))
        if not winners:
            return ActionResult.skipped("no_match")

        # (4) Highest score wins; ties broken by priority desc, then first-declared.
        winners.sort(key=lambda w: (-w[1], -int(w[2].get("priority", 0))))
        name, score, route = winners[0]
        dest = route.get("destination_folder")
        if not dest:
            return ActionResult.skipped("no_destination", classification=name)

        # (5) Move as the service principal; already-in-destination is a no-op.
        try:
            info = ctx.core.stat(file_uid)
            if getattr(info, "parent_uid", None) == dest:
                return ActionResult.skipped("already_there", classification=name,
                                            score=score, destination=dest)
        except Exception:
            ctx.log.warning("sorter: stat failed for %s", file_uid, exc_info=True)

        ok = ctx.core.move(file_uid, dest)
        if ok:
            return ActionResult.done(classification=name, score=score, destination=dest)
        return ActionResult.failed("move_failed", retryable=True,
                                   classification=name, destination=dest)

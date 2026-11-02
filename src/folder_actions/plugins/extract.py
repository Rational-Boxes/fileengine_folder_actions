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

"""Run extraction prompt sets on a folder's documents (convert_search_ai
EXTRACTION_PROMPT_SETS.md §6.1).

Fires when a file's text extraction completes (``conversion.complete`` with
``text``) or a file is moved in. Picks the prompt sets (fixed, or by the
document's classification with the folder sorter's classifier) and asks csai
to run them as jobs. It returns at once: the extraction itself runs in csai and
its outcome is an ``extraction.complete`` event, which the ``extraction_rules``
action (and anything else) acts on. Running the extraction here would stall
every other folder's events behind one document.

Revisions: a set an application publishes may get a new revision whenever the
application starts, so this binding pins the revision of every
application-managed set when it is saved (``pinned``); a new application
release never changes what a folder extracts. Builder-managed sets follow
their latest published revision unless pinned too.

Loop guard: a file the rules action filed is marked ``fa.filed_by =
<binding>@<version>``; the same version is not extracted again because it was
moved or copied (a copy is a new file with no cached values)."""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, field_validator

from ..classifier import document_classifier_simple
from ..csai_client import CsaiError, TextNotReady
from .base import ActionContext, ActionResult, FieldDescriptor, FieldOption, register

log = logging.getLogger("folder_actions.plugins.extract")

FILED_BY = "fa.filed_by"


@register
class ExtractAction:
    type_name = "extract"
    label = "Extract values with a prompt set"
    supported_events = frozenset({"conversion.complete", "file.moved"})

    class ConfigModel(BaseModel):
        mode: str = "fixed"                       # fixed | by_classification
        prompt_sets: list[str] = []
        classifier_set_id: str = ""
        by_label: dict[str, str] = {}             # classification -> prompt set
        pin_all: bool = False                     # pin builder sets too
        pinned: dict[str, int] = {}               # filled when the binding is saved
        markup: bool = True

        @field_validator("by_label", mode="before")
        @classmethod
        def _rows_to_map(cls, v):
            # The editor saves `by_label` as {label, prompt_set} rows (a group).
            if isinstance(v, list):
                return {str(r.get("label") or "").strip(): r.get("prompt_set") for r in v
                        if isinstance(r, dict) and r.get("label") and r.get("prompt_set")}
            return v

    @classmethod
    def config_fields(cls) -> list[FieldDescriptor]:
        return [
            FieldDescriptor(
                key="mode", label="Which prompt set", type="select", default="fixed",
                options=[FieldOption(value="fixed", label="Always these prompt sets"),
                         FieldOption(value="by_classification",
                                     label="Chosen by the document's classification")]),
            FieldDescriptor(
                key="prompt_sets", label="Prompt sets", type="multiselect",
                options_source="prompt_sets", visible_when={"key": "mode", "equals": "fixed"},
                help="Each runs on every document that arrives."),
            FieldDescriptor(
                key="classifier_set_id", label="Classifier set", type="ref",
                options_source="classifier_sets",
                visible_when={"key": "mode", "equals": "by_classification"}),
            FieldDescriptor(
                key="by_label", label="Prompt set per classification", type="group",
                visible_when={"key": "mode", "equals": "by_classification"},
                item_fields=[
                    FieldDescriptor(key="label", label="Classification", type="string",
                                    required=True),
                    FieldDescriptor(key="prompt_set", label="Prompt set", type="ref",
                                    required=True, options_source="prompt_sets"),
                ],
                help="The highest-scoring classification picks the set."),
            FieldDescriptor(
                key="pin_all", label="Pin the current revision of every set", type="boolean",
                default=False,
                help="Sets published by applications are always pinned when you save, so a "
                     "new application release never changes what this folder extracts."),
            FieldDescriptor(key="markup", label="Write the marked-up PDF", type="boolean",
                            default=True),
        ]

    # ------------------------------------------------------------------ on save
    @classmethod
    def prepare_config(cls, cfg: dict, csai, tenant: str) -> dict:
        """Pin revisions when the binding is saved (§4.5): every
        application-managed set, and every set when ``pin_all``. A set already
        pinned keeps its pin; one no longer chosen loses it."""
        m = cls.ConfigModel(**cfg)
        chosen = set(m.prompt_sets) if m.mode == "fixed" else set(m.by_label.values())
        try:
            current = {s["id"]: s for s in csai.list_prompt_sets(tenant)}
        except Exception:
            log.warning("extract: could not read prompt sets to pin revisions", exc_info=True)
            current = {}
        pinned = {k: v for k, v in m.pinned.items() if k in chosen}
        for sid in chosen:
            s = current.get(sid)
            if s and sid not in pinned and s.get("revision") and (
                    m.pin_all or s.get("managed") == "application"):
                pinned[sid] = int(s["revision"])
        out = dict(cfg)
        out["pinned"] = pinned
        return out

    # ------------------------------------------------------------------ run
    def _sets(self, file_uid: str, config, ctx) -> tuple[list[str], Optional[ActionResult]]:
        if config.mode != "by_classification":
            return list(config.prompt_sets), None
        try:
            text = ctx.csai.get_text(file_uid, ctx.tenant)
        except TextNotReady:
            return [], ActionResult.failed("text_not_ready", retryable=True, file_uid=file_uid)
        cset = ctx.store.get_classifier_set_full(ctx.tenant, config.classifier_set_id)
        if not cset:
            return [], ActionResult.failed("classifier_set_missing",
                                           classifier_set_id=config.classifier_set_id)
        scores = document_classifier_simple(text, [
            {"name": c["name"], "terms": [{"term": t["term"], "distance": t["distance"],
                                           "weight": t["weight"]} for t in c.get("terms", [])]}
            for c in cset.get("classifiers", [])])
        ranked = [(n, sc) for n, sc in scores.items() if sc > 0 and n in config.by_label]
        if not ranked:
            return [], ActionResult.skipped("no_classification_with_a_prompt_set")
        best = max(ranked, key=lambda x: x[1])[0]
        return [config.by_label[best]], None

    def execute(self, event: dict, config: "ExtractAction.ConfigModel",
                ctx: ActionContext) -> ActionResult:
        file_uid = event.get("file_uid") or ""
        if not file_uid:
            return ActionResult.skipped("no_file_uid")
        etype = event.get("type")
        if etype == "conversion.complete" and "text" not in (event.get("renditions") or []):
            return ActionResult.skipped("no_text")
        version = event.get("version") or ""
        try:
            info = ctx.core.stat(file_uid)
            version = version or getattr(info, "version", "") or ""
        except Exception:
            info = None
        marker = (ctx.core.metadata(file_uid) or {}).get(FILED_BY, "")
        if marker and version and marker.endswith("@" + version):
            return ActionResult.skipped("already_extracted_and_filed", marker=marker)
        sets, problem = self._sets(file_uid, config, ctx)
        if problem is not None:
            return problem
        if not sets:
            return ActionResult.skipped("no_prompt_set")
        jobs = []
        for sid in sets:
            try:
                jobs.append(ctx.csai.start_extract_job(
                    ctx.tenant, file_uid=file_uid, version=version, prompt_set=sid,
                    revision=config.pinned.get(sid), markup=config.markup,
                    binding_id=ctx.binding_id))
            except CsaiError as e:
                if e.retryable:
                    return ActionResult.failed("csai_unavailable", retryable=True,
                                               status=e.status, queued=jobs)
                return ActionResult.failed(e.kind or "csai_refused", status=e.status,
                                           prompt_set=sid, queued=jobs)
        return ActionResult.done(jobs=jobs, prompt_sets=sets,
                                 pinned={s: config.pinned[s] for s in sets if s in config.pinned})

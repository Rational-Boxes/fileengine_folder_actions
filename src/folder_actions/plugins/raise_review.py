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

"""Raise-a-review action (SPECIFICATIONS.md §7).

When a file is added to a bound folder (created, or moved in), automatically raise
a review request on that file assigned to the configured reviewer(s), via the
discussion service. Combined with the move-on-approve/reject action this composes
into a **chain across folders**: file lands -> review requested -> a human
approves/rejects -> the file moves to the next folder -> which raises the next
review. Because each hop waits on a human decision, the chain cannot loop on its own.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from .base import ActionContext, ActionResult, FieldDescriptor, register

log = logging.getLogger("folder_actions.plugins.raise_review")

# "File added" events (plus new-version/content-ready), the admin picks via on_events.
SUPPORTED = frozenset({"file.created", "file.moved", "file.updated", "conversion.complete"})


@register
class RaiseReviewAction:
    type_name = "raise_review"
    label = "Raise a review"
    supported_events = SUPPORTED

    class ConfigModel(BaseModel):
        # Encoded principals: bare uid/email for a user, "role:<name>" for a role
        # (each member becomes a reviewer).
        reviewers: list[str] = []

    @classmethod
    def config_fields(cls) -> list[FieldDescriptor]:
        return [
            FieldDescriptor(
                key="reviewers", label="Reviewers", type="principal", required=True,
                help="Who to request a review from on the added file. A role expands "
                     "to each of its members. Each reviewer must be able to read the file."),
        ]

    def execute(self, event: dict, config: "RaiseReviewAction.ConfigModel",
                ctx: ActionContext) -> ActionResult:
        file_uid = event.get("file_uid") or ""
        if not file_uid:
            return ActionResult.skipped("no_file")

        # Expand role:<name> to members; collect individual user reviewers.
        reviewers: list[str] = []
        for r in config.reviewers:
            r = (r or "").strip()
            if not r:
                continue
            if r.lower().startswith("role:"):
                reviewers.extend(ctx.core.users_for_role(r.split(":", 1)[1].strip()))
            else:
                reviewers.append(r)

        # De-dupe and never ask the actor to review their own addition.
        actor = (event.get("actor") or "").strip()
        seen: set[str] = set()
        final: list[str] = []
        for u in reviewers:
            u = (u or "").strip()
            if u and u != actor and u not in seen:
                seen.add(u)
                final.append(u)
        if not final:
            return ActionResult.skipped("no_reviewers")

        version = event.get("version") or ""
        status, body = ctx.discussion.raise_review(file_uid, final, ctx.tenant, version=version)
        if status in (200, 201):
            n = len(body.get("reviews") or []) or len(final)
            return ActionResult.done(file_uid=file_uid, reviewers=final, reviews=n)
        if status == 422:
            # One or more reviewers can't access the file (discussion error-marks them).
            return ActionResult.skipped("reviewers_no_access",
                                        invalid=body.get("detail") or body, reviewers=final)
        if status == 0:
            return ActionResult.failed("discussion_unreachable", retryable=True)
        return ActionResult.failed("raise_review_failed", status=status, body=body)

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

"""Move-on-review action (SPECIFICATIONS.md §7.1).

When a review on a file in the bound folder is approved or rejected, move the file
to the configured destination folder as the service principal. Either outcome may be
left unconfigured (no move for that outcome); a file already living in the
destination is a no-op."""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel

from .base import ActionContext, ActionResult, FieldDescriptor, register

log = logging.getLogger("folder_actions.plugins.move_review")


@register
class MoveReviewAction:
    type_name = "move_review"
    label = "Move on review approve/reject"
    supported_events = frozenset({"review.approved", "review.rejected"})

    class ConfigModel(BaseModel):
        on_approved: Optional[str] = None  # destination folder uid for approvals
        on_rejected: Optional[str] = None  # destination folder uid for rejections

    @classmethod
    def config_fields(cls) -> list[FieldDescriptor]:
        return [
            FieldDescriptor(
                key="on_approved", label="Move approved to", type="folder",
                required=False,
                help="Folder the file is moved to when its review is approved. "
                     "Leave empty to not move on approval.",
            ),
            FieldDescriptor(
                key="on_rejected", label="Move rejected to", type="folder",
                required=False,
                help="Folder the file is moved to when its review is rejected. "
                     "Leave empty to not move on rejection.",
            ),
        ]

    def execute(self, event: dict, config: "MoveReviewAction.ConfigModel",
                ctx: ActionContext) -> ActionResult:
        etype = event.get("type")
        file_uid = event.get("file_uid")
        if not file_uid:
            return ActionResult.skipped("no_file_uid")

        dest = config.on_approved if etype == "review.approved" else config.on_rejected
        if not dest:
            return ActionResult.skipped("no_destination", event=etype)

        # No-op if the file already lives in the destination (§7.1).
        try:
            info = ctx.core.stat(file_uid)
            if getattr(info, "parent_uid", None) == dest:
                return ActionResult.skipped("already_there", destination=dest)
        except Exception:  # stat is advisory here; fall through to the move
            ctx.log.warning("move_review: stat failed for %s", file_uid, exc_info=True)

        ok = ctx.core.move(file_uid, dest)
        if ok:
            return ActionResult.done(file_uid=file_uid, destination=dest, event=etype)
        return ActionResult.failed("move_failed", retryable=True,
                                   file_uid=file_uid, destination=dest)

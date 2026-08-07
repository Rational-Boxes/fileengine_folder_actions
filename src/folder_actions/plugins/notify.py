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

"""Notify user/group action (SPECIFICATIONS.md §7.2).

On any recognized event listed in ``events``, email the configured recipients in
real time (one email per event, not digested), best-effort via the SMTP mailer. A
``role:<name>`` recipient fans out to all members of the tenant role. The actor is
never self-notified; SMTP being unconfigured skips the run (logged, no email)."""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel

from .base import ActionContext, ActionResult, FieldDescriptor, FieldOption, register

log = logging.getLogger("folder_actions.plugins.notify")

# The recognized event types notify may bind to (SPECIFICATIONS.md §3.1).
SUPPORTED = frozenset({
    "file.created", "file.updated", "file.moved", "file.renamed", "file.deleted",
    "file.restored", "review.approved", "review.rejected", "thread.opened",
    "comment.created", "mention.created", "thread.resolved", "conversion.complete",
    "conversion.failed",
})


@register
class NotifyAction:
    type_name = "notify"
    label = "Notify a user or group"
    supported_events = SUPPORTED

    class ConfigModel(BaseModel):
        recipients: list[str] = []       # uids, emails, or "role:<name>"
        events: list[str] = []           # subset to fire on; empty => all supported
        template: Optional[str] = None   # optional body kind/override (§7.2)

    @classmethod
    def config_fields(cls) -> list[FieldDescriptor]:
        return [
            FieldDescriptor(
                key="recipients", label="Recipients", type="group", required=True,
                help="Users or roles to email. A role fans out to all its members.",
                item_fields=[
                    FieldDescriptor(key="principal", label="User or role",
                                    type="principal", required=True),
                ],
            ),
            FieldDescriptor(
                key="events", label="On events", type="multiselect",
                options_source="event_catalog",
                help="Which recognized events trigger a notification. "
                     "Empty means every supported event.",
            ),
            FieldDescriptor(
                key="template", label="Template", type="select", required=False,
                options=[FieldOption(value="default", label="Default")],
                help="Optional notification body override.",
            ),
        ]

    def execute(self, event: dict, config: "NotifyAction.ConfigModel",
                ctx: ActionContext) -> ActionResult:
        etype = event.get("type")
        allowed = set(config.events) if config.events else set(SUPPORTED)
        if etype not in allowed:
            return ActionResult.skipped("event_filtered", event=etype)

        if not ctx.mailer.configured:
            return ActionResult.skipped("smtp_unconfigured")

        # Expand role:<name> recipients to their members (§7.2).
        expanded: list[str] = []
        for r in config.recipients:
            r = (r or "").strip()
            if not r:
                continue
            if r.lower().startswith("role:"):
                expanded.extend(ctx.core.users_for_role(r.split(":", 1)[1].strip()))
            else:
                expanded.append(r)

        actor = (event.get("actor") or "").strip()
        file_uid = event.get("file_uid") or ""
        subject = f"[{event.get('tenant', ctx.tenant)}] {etype}"
        deep_link = self._deep_link(ctx, event)
        text, html = self._body(etype, event, deep_link)

        sent, seen = 0, set()
        skipped_self = 0
        for ident_str in expanded:
            identity = ctx.directory.resolve_principal(ident_str)
            # Skip self-notification: the acting user is not emailed (§7.2).
            if actor and (ident_str == actor or (identity and identity.user == actor)):
                skipped_self += 1
                continue
            email = (identity.email if identity and identity.email else
                     (ident_str if "@" in ident_str else ""))
            if not email or email in seen:
                continue
            seen.add(email)
            if ctx.mailer.send(email, subject, text, html):
                sent += 1

        if sent == 0:
            return ActionResult.skipped("no_recipients_emailed",
                                        event=etype, self_skipped=skipped_self)
        return ActionResult.done(event=etype, emailed=sent, file_uid=file_uid)

    @staticmethod
    def _deep_link(ctx: ActionContext, event: dict) -> str:
        base = (getattr(ctx.config, "frontend_base_url", "") or "").rstrip("/")
        file_uid = event.get("file_uid") or ""
        if not base or not file_uid:
            return base
        thread_id = event.get("thread_id")
        if thread_id:
            return f"{base}/files/{file_uid}?thread={thread_id}"
        version = event.get("version")
        if version:
            return f"{base}/files/{file_uid}?version={version}"
        return f"{base}/files/{file_uid}"

    @staticmethod
    def _body(etype: str, event: dict, link: str) -> tuple[str, str]:
        name = event.get("name") or event.get("file_uid") or "a file"
        actor = event.get("actor") or "someone"
        line = f"{actor} triggered {etype} on {name}."
        text = f"{line}\n\n{link}" if link else line
        html = (f"<p>{line}</p>"
                + (f'<p><a href="{link}">Open in the app</a></p>' if link else ""))
        return text, html

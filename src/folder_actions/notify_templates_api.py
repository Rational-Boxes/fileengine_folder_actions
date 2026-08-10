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

"""Event-notification email templates (used by the notify action, §7.2).

Tenant-level, reusable templates the notify action renders per event. subject/body
carry {placeholder} tokens: {actor} {event} {name} {file_uid} {version} {tenant}
{folder_uid} {link}. Mutations are tenant-admin; the LIST is available to any
authenticated user so the binding editor's template dropdown resolves.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .deps import identity, require_tenant_admin
from .ldap_auth import Identity

log = logging.getLogger("folder_actions.notify_templates_api")

router = APIRouter()


class TemplateCreate(BaseModel):
    name: str
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    # Set by the provisioning service (§14a) to mark the template externally managed.
    managed_by: Optional[str] = None


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    subject: Optional[str] = None
    body_text: Optional[str] = None
    body_html: Optional[str] = None


@router.get("/notify-templates")
def list_templates(request: Request, ident: Identity = Depends(identity)) -> list[dict]:
    # Any authenticated user may list (name/subject only) so the notify binding
    # editor can offer the template dropdown.
    return request.app.state.store.list_notify_templates(ident.tenant)


@router.post("/notify-templates")
def create_template(request: Request, body: TemplateCreate,
                    ident: Identity = Depends(require_tenant_admin)) -> dict:
    return request.app.state.store.create_notify_template(
        ident.tenant, name=body.name, subject=body.subject,
        body_text=body.body_text, body_html=body.body_html, created_by=ident.user,
        managed_by=body.managed_by)


@router.get("/notify-templates/{template_id}")
def get_template(template_id: str, request: Request,
                 ident: Identity = Depends(require_tenant_admin)) -> dict:
    tpl = request.app.state.store.get_notify_template(ident.tenant, template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="Notification template not found")
    return tpl


@router.put("/notify-templates/{template_id}")
def update_template(template_id: str, request: Request, body: TemplateUpdate,
                    ident: Identity = Depends(require_tenant_admin)) -> dict:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    updated = request.app.state.store.update_notify_template(ident.tenant, template_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="Notification template not found")
    return updated


@router.delete("/notify-templates/{template_id}")
def delete_template(template_id: str, request: Request,
                    ident: Identity = Depends(require_tenant_admin)) -> dict:
    request.app.state.store.delete_notify_template(ident.tenant, template_id)
    return {"deleted": True, "id": template_id}

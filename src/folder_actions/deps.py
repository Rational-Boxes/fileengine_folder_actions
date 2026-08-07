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

"""Request-scoped identity + authorization dependencies for the HTTP surface.

``identity`` resolves the caller (Basic/Bearer + tenant); ``require_tenant_admin``
gates the administrator-only surfaces (redaction §5b, invisible viewing §10h).

M0 note: admin is determined from the caller's resolved roles (``administrators`` /
``system_admin``, mapped at LDAP auth time — see ldap_auth.Identity.is_admin). A
stricter authoritative LDAP group check (as ldap_manager does) can replace this
without changing call sites.
"""
from fastapi import HTTPException, Request

from .http_auth import extract_tenant, resolve_identity
from .ldap_auth import Identity


def identity(request: Request) -> Identity:
    """Resolve the requesting user from Authorization (Basic/Bearer) + tenant, or 401."""
    config = request.app.state.config
    headers = {k.lower(): v for k, v in request.headers.items()}
    tenant = extract_tenant(headers, headers.get("host", ""), config.tenant)
    ident = resolve_identity(
        headers.get("authorization", ""), tenant, config,
        request.app.state.token_store,
        getattr(request.app.state, "bridge_verifier", None),
    )
    if ident is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return ident


def require_tenant_admin(request: Request) -> Identity:
    """Identity of a tenant administrator, or 403."""
    ident = identity(request)
    if not ident.is_admin:
        raise HTTPException(status_code=403, detail="tenant administrator required")
    return ident


def require_system_admin(request: Request) -> Identity:
    """Identity carrying the trusted ``system_admin`` role, or 403 — gates the
    internal RAG retrieve endpoint (§6), called service-to-service by CSAI's agent."""
    ident = identity(request)
    if "system_admin" not in ident.roles:
        raise HTTPException(status_code=403, detail="system administrator required")
    return ident

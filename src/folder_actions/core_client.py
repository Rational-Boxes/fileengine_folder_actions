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

"""FileEngine gRPC clients bound to an identity (trusted-upstream model).

Two identities (SPECIFICATIONS.md §7.5, §11):
  - **End user** — ``client_for(identity)``; used by the admin API to enforce ACLs
    on folder bindings *as the caller* (WRITE/MANAGE_ACL on the folder, §5).
  - **Service principal** — ``CoreClient`` wraps a client acting as the folder_actions
    agent account; every *action* mutation (Move/SetMetadata) runs as it, so
    automation may write to a destination the triggering user could not. It is **not**
    ``system_admin`` — its rights come from ACL grants — and it also drives
    loop-avoidance (the consumer ignores ``file.moved`` whose actor is this principal).

``fileengine`` is imported lazily so config/auth/health import without the gRPC stack.
"""
from __future__ import annotations

import contextvars
from typing import Optional

from .ldap_auth import Identity, authenticate

# Request-scoped client IP (set by the HTTP middleware), forwarded for core audit.
request_source_addr: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "request_source_addr", default="")


def client_for(identity: Identity, config):
    """A gRPC client that acts as ``identity`` (the end user)."""
    from ._client import ManagedFiles
    return ManagedFiles(
        server_address=config.grpc_address,
        user_name=identity.user,
        user_roles=identity.roles,
        tenant=identity.tenant or config.tenant,
        source_addr=request_source_addr.get(),
    )


def agent_identity(config) -> Identity:
    """Authenticate the service's own action principal against LDAP."""
    return authenticate(config, config.agent_user, config.agent_password)


def agent_client(config):
    """A gRPC client acting as the folder_actions service principal (§7.5).
    No ACL bypass — its write rights come from ACL grants on managed folders."""
    return client_for(agent_identity(config), config)


# Actor name the consumer uses to recognise (and ignore) self-generated moves (§3.3).
def service_actor(config) -> str:
    return config.agent_user or "svc:folder_actions"


class CoreClient:
    """Thin action-facing wrapper over a service-principal ``ManagedFiles``. Exposes
    only what the plug-ins need; all calls act as the service principal."""

    def __init__(self, config):
        self.config = config
        self.tenant = config.tenant
        self.actor = service_actor(config)
        self._mf = None

    def _client(self):
        if self._mf is None:
            self._mf = agent_client(self.config)
        return self._mf

    # -- reads --
    def stat(self, uid: str):
        return self._client().stat(uid, tenant=self.tenant)

    def parent_of(self, uid: str) -> str:
        return self.stat(uid).parent_uid

    def metadata(self, uid: str) -> dict:
        try:
            return self._client().get_metadata_values(uid, tenant=self.tenant)
        except Exception:
            return {}

    def read_prefix(self, uid: str, n: int = 8192) -> bytes:
        """First ``n`` bytes of the latest version — for content MIME sniffing (§7.4.1)."""
        buf = self._client().get(uid, tenant=self.tenant)
        try:
            return buf.read(n)
        finally:
            try:
                buf.close()
            except Exception:
                pass

    def users_for_role(self, role: str) -> list[str]:
        try:
            return list(self._client().get_users_for_role(role, tenant=self.tenant) or [])
        except Exception:
            return []

    # -- writes (as the service principal) --
    def move(self, file_uid: str, destination_folder: str) -> bool:
        return self._client().move(file_uid, destination_folder, tenant=self.tenant)

    def set_metadata(self, uid: str, key: str, value: str) -> bool:
        return self._client().set_metadata_value(uid, key, str(value), tenant=self.tenant)

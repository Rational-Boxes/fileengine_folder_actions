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
import logging
from typing import Optional

from .ldap_auth import Identity, authenticate

log = logging.getLogger("folder_actions.core_client")

# Internal fileengine::Permission bit flags (file_engine_core acl_manager.h) mapped
# to the proto Permission enum *name*. GetResourceAcls returns each rule's
# ``permissions`` as this internal bitmask, but Grant/RevokePermission take a single
# proto Permission enum — so a rule is copied bit-by-bit. These bit values are
# wire/DB-stable (the stored ACL rows use them).
_PERM_BIT_TO_NAME = {
    0x0001: "EXECUTE",
    0x0008: "RESTORE_TO_VERSION",
    0x0010: "RETRIEVE_BACK_VERSION",
    0x0020: "VIEW_VERSIONS",
    0x0040: "UNDELETE",
    0x0080: "LIST_DELETED",
    0x0100: "DELETE",
    0x0200: "WRITE",
    0x0400: "READ",
    0x0800: "MANAGE_ACL",
    0x1000: "ACL_INHERIT",
    0x2000: "CULL_VERSIONS",
}


def _prefixed_principal(principal: str, ptype: int) -> Optional[str]:
    """Rebuild the Grant/Revoke principal string from a stored (principal, type).

    The core strips the ``role:``/``claim:`` wire prefix at store time and records a
    PrincipalType (0 user, 1 role, 2 group[reserved], 3 other, 4 claim); Grant/Revoke
    re-derive the type from the prefix, so we re-attach it. OTHER ('everyone') is
    matched by type and kept verbatim; the reserved GROUP type is never created by
    the core, so we skip it (returns ``None``)."""
    if ptype == 1:   # ROLE
        return "role:" + principal
    if ptype == 4:   # CLAIM (principal is the bare "key=value")
        return "claim:" + principal
    if ptype == 3:   # OTHER — the 'everyone' catch-all, matched by type
        return principal
    if ptype == 2:   # GROUP — reserved/unused, no grantable prefix
        return None
    return principal  # USER


def _acl_atoms(acls: list[dict]) -> set[tuple[str, str, str]]:
    """Explode ACL entry dicts into a set of ``(prefixed_principal, effect, perm_name)``
    atoms — the unit Grant/Revoke operate on — so two ACLs can be diffed directly.
    Unmappable principals (reserved GROUP) and any unknown bits are skipped."""
    atoms: set[tuple[str, str, str]] = set()
    for e in acls:
        pp = _prefixed_principal(e.get("principal", ""), int(e.get("type", 0)))
        if pp is None:
            continue
        effect = e.get("effect", "allow")
        mask = int(e.get("permissions", 0))
        for bit, name in _PERM_BIT_TO_NAME.items():
            if mask & bit:
                atoms.add((pp, effect, name))
    return atoms

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

    def __init__(self, config, tenant: Optional[str] = None):
        self.config = config
        # folder_actions consumes the shared multi-tenant stream, so a core client is
        # bound to the *event's* tenant (falling back to the config default), not a
        # single fixed tenant — every op runs in that tenant's schema.
        self.tenant = tenant or config.tenant
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

    def listdir(self, uid: str) -> list:
        """Direct children of a folder as ``DirectoryEntry`` objects (the reconcile
        sweep's enumeration, §8). Soft-deleted entries are excluded — a deleted file
        is not work to recover. Returns ``[]`` for an empty or unlistable folder;
        ``dir()`` answers ``False`` for a non-directory, which normalizes to ``[]``."""
        entries = self._client().dir(uid, tenant=self.tenant)
        return list(entries) if entries else []

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

    def get_resource_acls(self, uid: str) -> list[dict]:
        """A resource's own explicit ACL rules (see client.get_resource_acls)."""
        return list(self._client().get_resource_acls(uid, tenant=self.tenant) or [])

    # -- writes (as the service principal) --
    def move(self, file_uid: str, destination_folder: str, *, normalize: bool = True) -> bool:
        """Move ``file_uid`` into ``destination_folder`` as the service principal.

        When ``normalize`` (default), a moved file's permissions are normalized to
        its new folder as a post-move followup (§7.7): the file keeps only the ACL
        profile of its destination, not stale grants from where it came from. The
        normalization is best-effort — it never fails or reverses a completed move
        (the file has already moved and the ``file.moved`` event has fired)."""
        ok = self._client().move(file_uid, destination_folder, tenant=self.tenant)
        if ok and normalize:
            try:
                self.normalize_permissions(file_uid, destination_folder)
            except Exception:
                log.warning("permission normalization failed for %s -> %s (move kept)",
                            file_uid, destination_folder, exc_info=True)
        return ok

    def grant_permission(self, uid: str, principal: str, permission, effect: str = "allow") -> bool:
        return self._client().grant_permission(uid, principal, permission, effect=effect,
                                               tenant=self.tenant)

    def revoke_permission(self, uid: str, principal: str, permission, effect: str = "allow") -> bool:
        return self._client().revoke_permission(uid, principal, permission, effect=effect,
                                                tenant=self.tenant)

    def normalize_permissions(self, file_uid: str, destination_folder: str) -> bool:
        """Mirror mode (§7.7): make the moved file's OWN ACL a copy of its destination
        folder's OWN ACL. Clears rules the file carried from its previous location and
        adds the destination's — reconciled as a minimal diff so a rule already correct
        on both (e.g. the service principal's MANAGE_ACL) is left untouched. Individual
        grant/revoke failures are logged and skipped; the method is best-effort."""
        target = _acl_atoms(self.get_resource_acls(destination_folder))
        current = _acl_atoms(self.get_resource_acls(file_uid))
        # Drop what the file carries that the destination profile doesn't have...
        for principal, effect, perm in sorted(current - target):
            try:
                self.revoke_permission(file_uid, principal, perm, effect)
            except Exception:
                log.warning("normalize: revoke %s/%s/%s on %s failed",
                            principal, perm, effect, file_uid, exc_info=True)
        # ...and add the destination's rules the file is missing.
        for principal, effect, perm in sorted(target - current):
            try:
                self.grant_permission(file_uid, principal, perm, effect)
            except Exception:
                log.warning("normalize: grant %s/%s/%s on %s failed",
                            principal, perm, effect, file_uid, exc_info=True)
        return True

    def set_metadata(self, uid: str, key: str, value: str) -> bool:
        return self._client().set_metadata_value(uid, key, str(value), tenant=self.tenant)

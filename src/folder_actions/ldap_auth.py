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

"""LDAP authentication and role resolution — the auth/permission authority.

Mirrors CSAI / the FileEngine MCP server: a real bind authenticates the user,
roles come from group membership, and a tenant's ``administrators`` group maps to
the core's ``system_admin`` role. The resolved identity is forwarded to the gRPC
core, which enforces ACLs.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from ldap3 import Server, Connection, ALL, SUBTREE
from ldap3.core.exceptions import LDAPException

from .failover import CircuitBreaker


@dataclass
class Identity:
    user: str
    roles: List[str] = field(default_factory=list)
    tenant: str = "default"
    authenticated: bool = False
    email: str = ""

    @property
    def is_admin(self) -> bool:
        """Tenant administrator (or system_admin) — gates redaction (§5b) and
        invisible viewing (§10h). ``administrators`` maps to ``system_admin`` at
        authentication time, so either marks an admin."""
        return "administrators" in self.roles or "system_admin" in self.roles


class _ServerUnreachable(Exception):
    """The directory server couldn't be reached (vs. a credential rejection)."""


_ldap_breaker: Optional[CircuitBreaker] = None


def _breaker(cfg) -> CircuitBreaker:
    global _ldap_breaker
    if _ldap_breaker is None:
        _ldap_breaker = CircuitBreaker(cooldown_s=getattr(cfg, "failover_cooldown_s", 30))
    return _ldap_breaker


def _ldap_targets(cfg):
    if not getattr(cfg, "ldap_replica_enabled", False):
        return [(cfg.ldap_uri, True)]
    if _breaker(cfg).should_try_primary():
        return [(cfg.ldap_uri, True), (cfg.ldap_uri_replica, False)]
    return [(cfg.ldap_uri_replica, False)]


def authenticate(cfg, username: str, password: str) -> Identity:
    """Bind as ``username`` to authenticate, then resolve roles from LDAP groups.

    Returns an Identity with ``authenticated=False`` if the bind fails or the user
    is not found. An unreachable master fails over to a configured replica."""
    ident = Identity(user=username, tenant=cfg.tenant)
    if not username or not password:
        return ident

    for uri, is_primary in _ldap_targets(cfg):
        try:
            result = _authenticate_against(uri, cfg, username, password)
            if is_primary:
                _breaker(cfg).reset()
            return result
        except _ServerUnreachable:
            if is_primary:
                _breaker(cfg).trip()
            continue
    return ident


def _authenticate_against(uri: str, cfg, username: str, password: str) -> Identity:
    ident = Identity(user=username, tenant=cfg.tenant)
    server = Server(uri, get_info=ALL)
    try:
        svc = Connection(server, cfg.ldap_bind_dn, cfg.ldap_bind_password, auto_bind=True)
    except LDAPException as e:
        raise _ServerUnreachable(uri) from e

    try:
        svc.search(cfg.ldap_user_base, f"(uid={username})", search_scope=SUBTREE, attributes=["cn"])
        if not svc.entries:
            return ident
        user_dn = svc.entries[0].entry_dn

        try:
            user_conn = Connection(server, user_dn, password, auto_bind=True)
            user_conn.unbind()
        except LDAPException:
            return ident

        roles: List[str] = []
        svc.search(cfg.ldap_tenant_base,
                   f"(&(objectClass=groupOfNames)(member={user_dn}))",
                   search_scope=SUBTREE, attributes=["cn"])
        for entry in svc.entries:
            cn = str(entry.cn)
            if cn and cn not in roles:
                roles.append(cn)

        if "administrators" in roles and "system_admin" not in roles:
            roles.append("system_admin")

        ident.roles = roles
        ident.authenticated = True
        return ident
    finally:
        svc.unbind()

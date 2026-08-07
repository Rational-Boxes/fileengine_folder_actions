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

"""Per-request credential resolution for the HTTP surface (mirrors CSAI).

Two credential paths, both ending at the same LDAP-derived identity:
  * ``Authorization: Basic <user:pass>``  → a live LDAP bind every request.
  * ``Authorization: Bearer <token>``     → our ``/auth/token`` token, or a bridge
    token verified via ``BridgeTokenVerifier``.

The tenant is per-session: ``X-Tenant`` header or a Host subdomain label, else the
configured default — independent of the user's LDAP entry.
"""
import base64
from dataclasses import replace
from typing import Optional, Tuple

from .ldap_auth import Identity, authenticate
from .token_store import TokenStore


def decode_basic(header_value: str) -> Optional[Tuple[str, str]]:
    if not header_value.startswith("Basic "):
        return None
    try:
        raw = base64.b64decode(header_value[len("Basic "):]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in raw:
        return None
    user, password = raw.split(":", 1)
    return user, password


def extract_tenant(headers: dict, host: str, default: str) -> str:
    explicit = headers.get("x-tenant")
    if explicit:
        return explicit.strip()
    host = (host or "").split(":", 1)[0]
    labels = host.split(".")
    if len(labels) >= 3:
        first = labels[0].strip().lower()
        if first and first not in ("www", "api", "localhost"):
            return first
    return default


def resolve_identity(auth_header: str, tenant: str, config, store: TokenStore,
                     bridge=None) -> Optional[Identity]:
    """Resolve an Authorization header to an authenticated Identity scoped to
    ``tenant``, or ``None`` if authentication fails / no credentials are given."""
    if not auth_header:
        return None
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):].strip()
        identity = store.resolve(token)
        if identity is not None:
            return replace(identity, tenant=tenant)
        if bridge is not None:
            return bridge.verify(token, tenant)
        return None
    basic = decode_basic(auth_header)
    if basic is None:
        return None
    identity = authenticate(config, basic[0], basic[1])
    if not identity.authenticated:
        return None
    return replace(identity, tenant=tenant)

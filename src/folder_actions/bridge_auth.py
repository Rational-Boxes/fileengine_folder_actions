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

"""Accept bridge-issued bearer tokens (mirrors CSAI's bridge_auth).

The http_bridge is the upstream token authority: a token it minted (LDAP or OAuth)
is verified here either locally (HS256, shared ``FILEENGINE_JWT_SECRET``) or by
calling its ``GET /v1/auth/introspect`` (cached briefly). One login authenticates
across both services. The request tenant (``X-Tenant``) is forwarded; the returned
identity is already tenant-scoped.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from .ldap_auth import Identity
from .jwt_verify import identity_from_claims, verify_hs256


class BridgeTokenVerifier:
    def __init__(self, base_url: str, ttl_seconds: int = 60, timeout: float = 3.0,
                 jwt_secret: str = ""):
        self.base_url = (base_url or "").rstrip("/")
        self.ttl = ttl_seconds
        self.timeout = timeout
        self.jwt_secret = jwt_secret or ""
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[Identity, float]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.jwt_secret) or bool(self.base_url)

    def verify(self, token: str, tenant: str) -> Optional[Identity]:
        if not token or not self.enabled:
            return None
        if self.jwt_secret:
            claims = verify_hs256(token, self.jwt_secret)
            if claims is None:
                return None
            got = identity_from_claims(claims, tenant)
            if got is None:
                return None
            user, roles = got
            return Identity(user=user, roles=roles,
                            tenant=tenant or claims.get("tenant", "default"),
                            authenticated=True)
        key = (token, tenant)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[1] > now:
                return hit[0]
        ident = self._introspect(token, tenant)
        if ident is not None:
            with self._lock:
                self._cache[key] = (ident, now + self.ttl)
        return ident

    def _introspect(self, token: str, tenant: str) -> Optional[Identity]:
        req = urllib.request.Request(self.base_url + "/v1/auth/introspect", method="GET")
        req.add_header("Authorization", "Bearer " + token)
        if tenant:
            req.add_header("X-Tenant", tenant)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status != 200:
                    return None
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if not data.get("active") or not data.get("user"):
            return None
        return Identity(
            user=data["user"],
            roles=list(data.get("roles") or []),
            tenant=tenant or data.get("tenant", "default"),
            authenticated=True,
        )

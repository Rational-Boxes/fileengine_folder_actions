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

"""Environment loading + Config for folder_actions (SPECIFICATIONS.md §9).

Shared cross-service knobs keep the ``FILEENGINE_*`` prefix (gRPC, LDAP, Redis, the
event stream, the JWT secret) so one login and one event bus span every service.
Service-private knobs use ``FA_*``. The service's own action identity (§7.5) is a
dedicated principal: ``FILEENGINE_FA_USER`` / ``FILEENGINE_FA_PASSWORD`` /
``FILEENGINE_FA_TENANT`` (falling back to the shared LDAP account)."""
from __future__ import annotations

import os


def load_dotenv(path: str = ".env") -> None:
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), _strip_value(val))


def _strip_value(val: str) -> str:
    val = val.strip()
    if val[:1] in ("'", '"'):
        q = val[0]
        end = val.find(q, 1)
        return val[1:end] if end != -1 else val[1:]
    if val.startswith("#"):
        return ""
    hi = val.find(" #")
    if hi != -1:
        val = val[:hi]
    return val.strip()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _first(*keys_and_default: str) -> str:
    *keys, default = keys_and_default
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v
    return default


def _bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


class Config:
    def __init__(self) -> None:
        # --- gRPC core (SHARED) ---
        self.grpc_host = _env("FILEENGINE_GRPC_HOST", "localhost")
        self.grpc_port = _env("FILEENGINE_GRPC_PORT", "50051")
        self.grpc_address = f"{self.grpc_host}:{self.grpc_port}"

        # --- Tenant + this service's own action principal (§7.5) ---
        self.tenant = _env("FILEENGINE_FA_TENANT", "default")
        self.agent_user = _first("FILEENGINE_FA_USER", "FILEENGINE_LDAP_USER", "")
        self.agent_password = _first("FILEENGINE_FA_PASSWORD", "FILEENGINE_LDAP_PASSWORD", "")

        # --- LDAP (SHARED) ---
        self.ldap_uri = _env("FILEENGINE_LDAP_ENDPOINT", "ldap://localhost:1389")
        self.ldap_uri_replica = _env("FILEENGINE_LDAP_ENDPOINT_REPLICA", "")
        if not self.ldap_uri_replica and _bool("FILEENGINE_LDAP_REPLICA_ENABLED", False):
            self.ldap_uri_replica = "ldap://localhost:1389"
        self.ldap_replica_enabled = bool(self.ldap_uri_replica)
        self.ldap_domain = _env("FILEENGINE_LDAP_DOMAIN", "dc=rationalboxes,dc=com")
        self.ldap_user_base = _env("FILEENGINE_LDAP_USER_BASE", "ou=users,dc=rationalboxes,dc=com")
        self.ldap_tenant_base = _env("FILEENGINE_LDAP_TENANT_BASE", "ou=tenants,dc=rationalboxes,dc=com")
        self.ldap_bind_dn = _env("FILEENGINE_LDAP_BIND_DN", "cn=admin,dc=rationalboxes,dc=com")
        self.ldap_bind_password = _env("FILEENGINE_LDAP_BIND_PASSWORD", "admin")

        # --- This service's own Postgres (PRIVATE FA_*) ---
        self.pg_host = _env("FA_PG_HOST", "localhost")
        self.pg_port = _int("FA_PG_PORT", 5432)
        self.pg_database = _env("FA_PG_DATABASE", "folder_actions")
        self.pg_user = _env("FA_PG_USER", "fileengine_user")
        self.pg_password = _env("FA_PG_PASSWORD", "fileengine_password")
        self.pg_replica_host = _env("FA_PG_REPLICA_HOST", "")
        if not self.pg_replica_host and _bool("FA_PG_REPLICA_ENABLED", False):
            self.pg_replica_host = "localhost"
        self.pg_replica_enabled = bool(self.pg_replica_host)
        self.pg_replica_port = _int("FA_PG_REPLICA_PORT", self.pg_port)
        self.pg_replica_database = _env("FA_PG_REPLICA_DATABASE", self.pg_database)
        self.pg_replica_user = _env("FA_PG_REPLICA_USER", self.pg_user)
        self.pg_replica_password = _env("FA_PG_REPLICA_PASSWORD", self.pg_password)
        self.failover_cooldown_s = _int("FA_FAILOVER_COOLDOWN_S", 30)
        self.db_statement_timeout_ms = _int("FA_DB_STATEMENT_TIMEOUT_MS", 5000)

        # --- HTTP surface (PRIVATE) — loopback by default (§9 monitoring) ---
        self.http_host = _env("FA_HTTP_HOST", "127.0.0.1")
        self.http_port = _int("FA_HTTP_PORT", 8099)
        self.cors_origins = [o.strip() for o in _env("FA_CORS_ORIGINS", "").split(",") if o.strip()]

        # --- Auth coordination (accept http_bridge bearer tokens) ---
        self.bridge_url = _env("FA_BRIDGE_URL", "")
        self.bridge_introspect_ttl = _int("FA_BRIDGE_INTROSPECT_TTL", 60)
        self.jwt_secret = _env("FILEENGINE_JWT_SECRET", "")  # SHARED
        self.token_ttl = _int("FA_TOKEN_TTL", 3600)
        self.permission_cache_ttl = _int("FA_PERMISSION_CACHE_TTL", 300)

        # --- Events: consume the shared core stream, own private group ---
        self.redis_host = _env("FILEENGINE_REDIS_HOST", "localhost")
        self.redis_port = _int("FILEENGINE_REDIS_PORT", 6379)
        self.redis_password = _env("FILEENGINE_REDIS_PASSWORD", "")
        self.redis_db = _int("FILEENGINE_REDIS_DB", 0)
        self.events_stream = _env("FILEENGINE_EVENTS_STREAM", "fileengine:events")  # SHARED
        self.events_group = _env("FA_EVENTS_GROUP", "folder_actions")               # PRIVATE

        # --- convert_search_ai (sorter text + rendition convert) ---
        self.csai_base_url = _env("FA_CSAI_BASE_URL", "http://localhost:8092")
        self.csai_timeout_s = _int("FA_CSAI_TIMEOUT_S", 30)

        # --- discussion (raise_review action) ---
        self.discuss_base_url = _env("FA_DISCUSS_BASE_URL", "http://localhost:8094")
        self.discuss_timeout_s = _int("FA_DISCUSS_TIMEOUT_S", 15)

        # --- Plug-ins + webhook secret box ---
        self.enabled_actions = {a.strip() for a in _env("FA_ENABLED_ACTIONS", "").split(",") if a.strip()}
        self.secret_key = _env("FA_SECRET_KEY", "")   # Fernet/AES key for webhook secrets at rest

        # --- SMTP (PRIVATE) — notify action ---
        self.smtp_host = _env("FA_SMTP_HOST", "")
        self.smtp_port = _int("FA_SMTP_PORT", 587)
        self.smtp_user = _env("FA_SMTP_USER", "")
        self.smtp_password = _env("FA_SMTP_PASSWORD", "")
        self.smtp_from = _env("FA_SMTP_FROM", "")
        # SPA base for deep-links in notification email bodies.
        self.frontend_base_url = _env("FA_FRONTEND_BASE_URL", "")

        # --- Consumer tuning ---
        self.consumer_name = _env("FA_CONSUMER_NAME", "worker-1")
        self.action_max_retries = _int("FA_ACTION_MAX_RETRIES", 5)

        # --- Reconcile sweep (§8) ---
        self.reconcile_enabled = _bool("FA_RECONCILE_ENABLED", True)
        self.reconcile_interval_s = _int("FA_RECONCILE_INTERVAL_S", 900)
        # How far back a sweep looks. Normally the window starts at the previous
        # sweep's watermark (minus an overlap, so a file modified mid-sweep isn't
        # missed); after a long outage — or on the very first sweep — it is clamped
        # to this maximum so one sweep can't walk an unbounded history.
        self.reconcile_lookback_s = _int("FA_RECONCILE_LOOKBACK_S", 86400)
        self.reconcile_overlap_s = _int("FA_RECONCILE_OVERLAP_S", 300)
        # Bounds on one sweep's work, per tenant. Hitting either is logged, never silent.
        self.reconcile_max_files = _int("FA_RECONCILE_MAX_FILES", 5000)
        self.reconcile_max_depth = _int("FA_RECONCILE_MAX_DEPTH", 32)

    # SmtpMailer (copied from discussion) reads ``digest_from``; alias it.
    @property
    def digest_from(self) -> str:
        return self.smtp_from

    def _dsn(self, host, port, database, user, password) -> str:
        return f"host={host} port={port} dbname={database} user={user} password={password}"

    @property
    def pg_dsn(self) -> str:
        return self._dsn(self.pg_host, self.pg_port, self.pg_database, self.pg_user, self.pg_password)

    @property
    def pg_replica_dsn(self) -> str:
        return self._dsn(self.pg_replica_host, self.pg_replica_port, self.pg_replica_database,
                         self.pg_replica_user, self.pg_replica_password)

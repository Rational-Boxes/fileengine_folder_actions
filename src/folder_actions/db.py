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

"""Postgres access — per-tenant schema isolation (mirrors CSAI's db.py).

Each tenant's data lives in its own ``tenant_<tenant>`` schema (see schema.py).
Connections set ``search_path`` to the tenant's schema so queries are unqualified
and naturally scoped to one tenant.

``psycopg`` is imported lazily so the package imports without it (M1+ runtime dep).
"""
from __future__ import annotations

from typing import Optional

from .config import Config
from .failover import CircuitBreaker, DegradedReadOnly
from .schema import ensure_tenant_schema, schema_name

_breaker: Optional[CircuitBreaker] = None


def _get_breaker(config: Config) -> CircuitBreaker:
    global _breaker
    if _breaker is None:
        _breaker = CircuitBreaker(cooldown_s=getattr(config, "failover_cooldown_s", 30))
    return _breaker


def connect(config: Config, readonly: bool = False):
    """Open a psycopg connection. With no replica configured this is the master.
    With a replica, the master is primary for reads + writes; if unreachable,
    reads fall back to the read-only replica and writes raise DegradedReadOnly."""
    import psycopg

    if not getattr(config, "pg_replica_enabled", False):
        return psycopg.connect(config.pg_dsn)

    breaker = _get_breaker(config)
    op_error = getattr(psycopg, "OperationalError", Exception)

    if not readonly:  # WRITE — master only
        if not breaker.should_try_primary():
            raise DegradedReadOnly("primary database unavailable — read-only fallback mode")
        try:
            conn = psycopg.connect(config.pg_dsn)
            breaker.reset()
            return conn
        except op_error as e:
            breaker.trip()
            raise DegradedReadOnly("primary database unavailable — read-only fallback mode") from e

    if breaker.should_try_primary():
        try:
            conn = psycopg.connect(config.pg_dsn)
            breaker.reset()
            return conn
        except op_error:
            breaker.trip()
    return psycopg.connect(config.pg_replica_dsn)


def provision_tenant(config: Config, tenant: str) -> str:
    """Ensure the tenant's schema + tables exist (idempotent). Returns the schema name."""
    conn = connect(config)
    try:
        return ensure_tenant_schema(conn, tenant)
    finally:
        conn.close()


# Tenants whose schema has been ensured in this process.
_provisioned: set[str] = set()


def connect_for_tenant(config: Config, tenant: str, provision: bool = False, readonly: bool = False):
    """A connection whose ``search_path`` is the tenant's schema (then ``public``).
    The schema is ensured on the first connection to a tenant in this process (and
    whenever ``provision=True``). ``readonly=True`` routes reads to the replica during
    a master outage and skips schema DDL."""
    conn = connect(config, readonly=readonly)
    # Provision on demand — including read-only reads, so a tenant whose *first*
    # request is a dashboard/search read doesn't hit missing tables. Skip DDL only
    # when this connection is a read-only replica (a standby can't run it; the
    # master keeps the schema in sync).
    on_replica = readonly and getattr(config, "pg_replica_enabled", False)
    if (provision or tenant not in _provisioned) and not on_replica:
        name = ensure_tenant_schema(conn, tenant)
        _provisioned.add(tenant)
    else:
        name = schema_name(tenant)
    with conn.cursor() as cur:
        cur.execute(f'SET search_path TO "{name}", public')
        timeout = int(getattr(config, "db_statement_timeout_ms", 0) or 0)
        if timeout > 0:
            cur.execute(f"SET statement_timeout = {timeout}")
    conn.commit()
    return conn

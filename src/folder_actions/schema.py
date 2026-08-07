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

"""Per-tenant Postgres schema for folder_actions (SPECIFICATIONS.md §10).

The schema *is* the tenant (``tenant_<slug>``); tables carry no tenant column.
DDL is idempotent (``IF NOT EXISTS`` + ``ADD COLUMN IF NOT EXISTS``) so a cold
tenant is provisioned on first touch and older tenants self-heal. ``gen_random_uuid()``
is native in PostgreSQL 13+ (no extension needed). NOTE: this string is fed through
``str.format`` — every literal ``{`` / ``}`` must be doubled."""
from __future__ import annotations

import re

_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


def schema_name(tenant: str) -> str:
    t = (tenant or "").strip()
    return "tenant_default" if not t else "tenant_" + _UNSAFE.sub("_", t)


_TENANT_DDL = '''
CREATE SCHEMA IF NOT EXISTS "{schema}";

-- One (folder, event, action) rule.
CREATE TABLE IF NOT EXISTS "{schema}".action_binding (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    folder_uid   text NOT NULL,
    recursive    boolean NOT NULL DEFAULT false,
    action_type  text NOT NULL,
    on_events    text[] NOT NULL DEFAULT '{{}}',
    mime_types   text[] NOT NULL DEFAULT '{{}}',
    config       jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    enabled      boolean NOT NULL DEFAULT true,
    created_by   text NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
-- Self-heal tenants provisioned before mime_types existed.
ALTER TABLE "{schema}".action_binding ADD COLUMN IF NOT EXISTS mime_types text[] NOT NULL DEFAULT '{{}}';
CREATE INDEX IF NOT EXISTS action_binding_folder_idx
    ON "{schema}".action_binding (folder_uid) WHERE enabled;

-- Reusable classifier sets (SmolDocBot), authored or imported.
CREATE TABLE IF NOT EXISTS "{schema}".classifier_set (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    created_by  text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS "{schema}".classifier (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    set_id    uuid NOT NULL REFERENCES "{schema}".classifier_set(id) ON DELETE CASCADE,
    name      text NOT NULL,
    position  integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS classifier_set_idx ON "{schema}".classifier (set_id);
CREATE TABLE IF NOT EXISTS "{schema}".classifier_term (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    classifier_id  uuid NOT NULL REFERENCES "{schema}".classifier(id) ON DELETE CASCADE,
    term           text NOT NULL,
    distance       integer NOT NULL DEFAULT 0,
    weight         double precision NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS classifier_term_idx ON "{schema}".classifier_term (classifier_id);

-- Per-binding routing over a classifier set (classification -> threshold + dest).
CREATE TABLE IF NOT EXISTS "{schema}".sorter_route (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    binding_id           uuid NOT NULL REFERENCES "{schema}".action_binding(id) ON DELETE CASCADE,
    classifier_set_id    uuid REFERENCES "{schema}".classifier_set(id) ON DELETE SET NULL,
    classification_name  text NOT NULL,
    threshold            double precision NOT NULL DEFAULT 0,
    destination_folder   text NOT NULL,
    priority             integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sorter_route_binding_idx ON "{schema}".sorter_route (binding_id);

-- Encrypted webhook credentials (never returned by the API).
CREATE TABLE IF NOT EXISTS "{schema}".webhook_secret (
    binding_id  uuid PRIMARY KEY REFERENCES "{schema}".action_binding(id) ON DELETE CASCADE,
    ciphertext  bytea NOT NULL
);

-- Reusable event-notification email templates (used by the notify action).
-- subject/body carry placeholder tokens (in braces): actor, event, name,
-- file_uid, version, tenant, folder_uid, link.
CREATE TABLE IF NOT EXISTS "{schema}".notify_template (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    subject     text NOT NULL DEFAULT '',
    body_text   text NOT NULL DEFAULT '',
    body_html   text NOT NULL DEFAULT '',
    created_by  text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Idempotency + execution log. One row per (event, binding).
CREATE TABLE IF NOT EXISTS "{schema}".action_run (
    event_id    text NOT NULL,
    binding_id  uuid NOT NULL,
    action_type text NOT NULL DEFAULT '',
    file_uid    text NOT NULL DEFAULT '',
    version     text NOT NULL DEFAULT '',
    status      text NOT NULL,
    detail      jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    ts          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, binding_id)
);
CREATE INDEX IF NOT EXISTS action_run_binding_ts_idx ON "{schema}".action_run (binding_id, ts DESC);
CREATE INDEX IF NOT EXISTS action_run_file_idx ON "{schema}".action_run (file_uid);
'''


def tenant_ddl(tenant: str) -> str:
    return _TENANT_DDL.format(schema=schema_name(tenant))


def ensure_tenant_schema(conn, tenant: str) -> str:
    name = schema_name(tenant)
    with conn.cursor() as cur:
        cur.execute(tenant_ddl(tenant))
    conn.commit()
    return name

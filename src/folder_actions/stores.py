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

"""Postgres access for folder_actions (SPECIFICATIONS.md §10).

Per-tenant schema (the schema *is* the tenant). Bindings, classifier sets, sorter
routes, encrypted webhook secrets, and the idempotent action_run log. All methods
open a short-lived connection via ``connect_for_tenant`` and commit before return."""
from __future__ import annotations

from typing import Any, Optional

from psycopg.types.json import Json

from .db import connect_for_tenant


def _row(cur) -> Optional[dict]:
    r = cur.fetchone()
    if r is None:
        return None
    cols = [d.name for d in cur.description]
    return dict(zip(cols, r))


def _rows(cur) -> list[dict]:
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class Store:
    def __init__(self, config):
        self.config = config

    def _conn(self, readonly: bool = False):
        return connect_for_tenant(self.config, self.config.tenant, readonly=readonly)

    def _conn_t(self, tenant: str, readonly: bool = False):
        return connect_for_tenant(self.config, tenant, readonly=readonly)

    # ---------------- bindings ----------------
    def create_binding(self, tenant: str, *, folder_uid: str, action_type: str,
                        on_events: list[str], config: dict, recursive: bool = False,
                        mime_types: Optional[list[str]] = None, created_by: str = "") -> dict:
        conn = self._conn_t(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO action_binding "
                    "(folder_uid, recursive, action_type, on_events, mime_types, config, created_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                    (folder_uid, recursive, action_type, on_events, mime_types or [],
                     Json(config), created_by))
                out = _row(cur)
            conn.commit()
            return out
        finally:
            conn.close()

    def get_binding(self, tenant: str, binding_id: str) -> Optional[dict]:
        conn = self._conn_t(tenant, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM action_binding WHERE id = %s", (binding_id,))
                return _row(cur)
        finally:
            conn.close()

    def list_bindings_for_folder(self, tenant: str, folder_uid: str) -> list[dict]:
        conn = self._conn_t(tenant, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM action_binding WHERE folder_uid = %s "
                            "ORDER BY created_at", (folder_uid,))
                return _rows(cur)
        finally:
            conn.close()

    def list_enabled_bindings(self, tenant: str) -> list[dict]:
        """All enabled bindings for the tenant — the consumer matches events against
        these (folder scoping applied in matching.py)."""
        conn = self._conn_t(tenant, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM action_binding WHERE enabled")
                return _rows(cur)
        finally:
            conn.close()

    def update_binding(self, tenant: str, binding_id: str, **fields) -> Optional[dict]:
        allowed = {"recursive", "on_events", "mime_types", "config", "enabled", "action_type"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k} = %s")
            vals.append(Json(v) if k == "config" else v)
        if not sets:
            return self.get_binding(tenant, binding_id)
        sets.append("updated_at = now()")
        conn = self._conn_t(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE action_binding SET {', '.join(sets)} WHERE id = %s RETURNING *",
                            (*vals, binding_id))
                out = _row(cur)
            conn.commit()
            return out
        finally:
            conn.close()

    def delete_binding(self, tenant: str, binding_id: str) -> bool:
        conn = self._conn_t(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM action_binding WHERE id = %s", (binding_id,))
                n = cur.rowcount
            conn.commit()
            return n > 0
        finally:
            conn.close()

    # ---------------- sorter routes ----------------
    def get_routes(self, tenant: str, binding_id: str) -> list[dict]:
        conn = self._conn_t(tenant, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM sorter_route WHERE binding_id = %s "
                            "ORDER BY priority DESC, classification_name", (binding_id,))
                return _rows(cur)
        finally:
            conn.close()

    def set_routes(self, tenant: str, binding_id: str, routes: list[dict]) -> None:
        """Replace a binding's routing table. Each route: classifier_set_id,
        classification_name, threshold, destination_folder, priority."""
        conn = self._conn_t(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sorter_route WHERE binding_id = %s", (binding_id,))
                for r in routes:
                    cur.execute(
                        "INSERT INTO sorter_route (binding_id, classifier_set_id, "
                        "classification_name, threshold, destination_folder, priority) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (binding_id, r.get("classifier_set_id"), r["classification_name"],
                         float(r.get("threshold", 0)), r["destination_folder"],
                         int(r.get("priority", 0))))
            conn.commit()
        finally:
            conn.close()

    # ---------------- classifier sets ----------------
    def create_classifier_set(self, tenant: str, name: str, created_by: str = "",
                              managed_by: Optional[str] = None) -> str:
        conn = self._conn_t(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO classifier_set (name, created_by, managed_by) "
                            "VALUES (%s,%s,%s) RETURNING id", (name, created_by, managed_by))
                sid = cur.fetchone()[0]
            conn.commit()
            return str(sid)
        finally:
            conn.close()

    def add_classifier(self, tenant: str, set_id: str, name: str, position: int = 0) -> str:
        conn = self._conn_t(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO classifier (set_id, name, position) VALUES (%s,%s,%s) "
                            "RETURNING id", (set_id, name, position))
                cid = cur.fetchone()[0]
            conn.commit()
            return str(cid)
        finally:
            conn.close()

    def add_term(self, tenant: str, classifier_id: str, term: str, distance: int, weight: float) -> None:
        conn = self._conn_t(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO classifier_term (classifier_id, term, distance, weight) "
                            "VALUES (%s,%s,%s,%s)", (classifier_id, term, int(distance), float(weight)))
            conn.commit()
        finally:
            conn.close()

    def list_classifier_sets(self, tenant: str) -> list[dict]:
        conn = self._conn_t(tenant, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, created_by, managed_by, created_at, updated_at "
                            "FROM classifier_set ORDER BY name")
                return _rows(cur)
        finally:
            conn.close()

    def get_classifier_set_full(self, tenant: str, set_id: str) -> Optional[dict]:
        """The set with nested classifiers+terms — for scoring and YAML export.
        Shape: {id, name, classifiers:[{id, name, terms:[{term, distance, weight}]}]}."""
        conn = self._conn_t(tenant, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, managed_by FROM classifier_set WHERE id = %s", (set_id,))
                head = _row(cur)
                if head is None:
                    return None
                cur.execute("SELECT id, name FROM classifier WHERE set_id = %s "
                            "ORDER BY position, name", (set_id,))
                classifiers = _rows(cur)
                for c in classifiers:
                    cur.execute("SELECT term, distance, weight FROM classifier_term "
                                "WHERE classifier_id = %s", (c["id"],))
                    c["terms"] = _rows(cur)
                head["classifiers"] = classifiers
                return head
        finally:
            conn.close()

    def delete_classifier_set(self, tenant: str, set_id: str) -> bool:
        conn = self._conn_t(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM classifier_set WHERE id = %s", (set_id,))
                n = cur.rowcount
            conn.commit()
            return n > 0
        finally:
            conn.close()

    # ---------------- notify templates ----------------
    def create_notify_template(self, tenant: str, *, name: str, subject: str = "",
                               body_text: str = "", body_html: str = "",
                               created_by: str = "", managed_by: Optional[str] = None) -> dict:
        conn = self._conn_t(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO notify_template (name, subject, body_text, body_html, created_by, managed_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                    (name, subject, body_text, body_html, created_by, managed_by))
                out = _row(cur)
            conn.commit()
            return out
        finally:
            conn.close()

    def list_notify_templates(self, tenant: str) -> list[dict]:
        conn = self._conn_t(tenant, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, subject, created_by, managed_by, created_at, updated_at "
                            "FROM notify_template ORDER BY name")
                return _rows(cur)
        finally:
            conn.close()

    def get_notify_template(self, tenant: str, template_id: str) -> Optional[dict]:
        conn = self._conn_t(tenant, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM notify_template WHERE id = %s", (template_id,))
                return _row(cur)
        finally:
            conn.close()

    def update_notify_template(self, tenant: str, template_id: str, **fields) -> Optional[dict]:
        allowed = {"name", "subject", "body_text", "body_html"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = %s"); vals.append(v)
        if not sets:
            return self.get_notify_template(tenant, template_id)
        sets.append("updated_at = now()")
        conn = self._conn_t(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE notify_template SET {', '.join(sets)} WHERE id = %s RETURNING *",
                            (*vals, template_id))
                out = _row(cur)
            conn.commit()
            return out
        finally:
            conn.close()

    def delete_notify_template(self, tenant: str, template_id: str) -> bool:
        conn = self._conn_t(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM notify_template WHERE id = %s", (template_id,))
                n = cur.rowcount
            conn.commit()
            return n > 0
        finally:
            conn.close()

    # ---------------- webhook secrets ----------------
    def put_secret(self, tenant: str, binding_id: str, ciphertext: bytes) -> None:
        conn = self._conn_t(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO webhook_secret (binding_id, ciphertext) VALUES (%s,%s) "
                    "ON CONFLICT (binding_id) DO UPDATE SET ciphertext = EXCLUDED.ciphertext",
                    (binding_id, ciphertext))
            conn.commit()
        finally:
            conn.close()

    def get_secret(self, tenant: str, binding_id: str) -> Optional[bytes]:
        conn = self._conn_t(tenant, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT ciphertext FROM webhook_secret WHERE binding_id = %s",
                            (binding_id,))
                r = cur.fetchone()
                return bytes(r[0]) if r else None
        finally:
            conn.close()

    # ---------------- tenants ----------------
    def list_tenants(self) -> list[str]:
        """Every tenant this service has provisioned, derived from the schema list.

        The schema *is* the tenant (schema.py), so ``tenant_<slug>`` schemas are the
        authoritative set for a service-wide job like the reconcile sweep — there is
        no tenant registry table. Note ``schema_name`` is lossy (it replaces unsafe
        characters with ``_``), so the slug is what round-trips back through it, not
        necessarily the original LDAP tenant string."""
        conn = self._conn(readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name LIKE 'tenant\\_%' ORDER BY schema_name")
                return [r[0][len("tenant_"):] for r in cur.fetchall()]
        finally:
            conn.close()

    # ---------------- reconcile watermark ----------------
    def get_reconcile_watermark(self, tenant: str):
        """When the last completed sweep for this tenant started (or ``None``)."""
        conn = self._conn_t(tenant, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT last_swept_at FROM reconcile_state WHERE id = 1")
                r = cur.fetchone()
                return r[0] if r else None
        finally:
            conn.close()

    def set_reconcile_watermark(self, tenant: str, when) -> None:
        conn = self._conn_t(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO reconcile_state (id, last_swept_at) VALUES (1, %s) "
                    "ON CONFLICT (id) DO UPDATE SET last_swept_at = EXCLUDED.last_swept_at",
                    (when,))
            conn.commit()
        finally:
            conn.close()

    # ---------------- action_run (idempotency + log) ----------------
    def run_exists(self, tenant: str, event_id: str, binding_id: str) -> bool:
        conn = self._conn_t(tenant, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM action_run WHERE event_id = %s AND binding_id = %s",
                            (event_id, binding_id))
                return cur.fetchone() is not None
        finally:
            conn.close()

    def run_covers_file(self, tenant: str, binding_id: str, file_uid: str,
                        version: str, since=None) -> bool:
        """Has ``binding_id`` already done this file's work, under *any* event id?

        The reconcile sweep's idempotency guard (§8). Event-id dedupe cannot serve
        there: the sweep synthesizes its own event ids, so it never collides with the
        live consumer's core-published ones and every recovered file would run twice.

        Two ways a run counts as covering the file:

        - **Same version** — the §8 ``(file_uid, version)`` content collapse.
        - **Ran after the file last changed** (``since`` = the file's ``modified_at``).
          Version equality alone is *not* sufficient, and assuming it was is a real
          double-run bug: a core ``file.created`` event carries an EMPTY version (the
          file has no content yet), while the sweep stamps the version it reads back
          from ``stat`` — so the keys never match and the action fires a second time.
          "A run already happened after the last modification" is the invariant that
          actually holds, independent of how each path fills in ``version``.

        Biased toward re-firing: if ``since`` is unknown the time clause is dropped,
        because a spurious repeat (bounded by the deterministic event id) is cheaper
        than silently dropping work the sweep exists to recover."""
        clauses = ["binding_id = %s", "file_uid = %s"]
        params: list[Any] = [binding_id, file_uid]
        covered = ["version = %s"]
        params.append(version)
        if since is not None:
            covered.append("ts >= %s")
            params.append(since)
        clauses.append("(" + " OR ".join(covered) + ")")

        conn = self._conn_t(tenant, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT 1 FROM action_run WHERE {' AND '.join(clauses)} LIMIT 1",
                    tuple(params))
                return cur.fetchone() is not None
        finally:
            conn.close()

    def record_run(self, tenant: str, *, event_id: str, binding_id: str, action_type: str,
                   file_uid: str, version: str, status: str, detail: dict) -> None:
        conn = self._conn_t(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO action_run "
                    "(event_id, binding_id, action_type, file_uid, version, status, detail) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (event_id, binding_id) DO UPDATE SET "
                    "status = EXCLUDED.status, detail = EXCLUDED.detail, ts = now()",
                    (event_id, binding_id, action_type, file_uid, version, status, Json(detail)))
            conn.commit()
        finally:
            conn.close()

    def list_runs(self, tenant: str, *, binding_id: Optional[str] = None,
                  file_uid: Optional[str] = None, limit: int = 100) -> list[dict]:
        conn = self._conn_t(tenant, readonly=True)
        try:
            where, params = [], []
            if binding_id:
                where.append("binding_id = %s"); params.append(binding_id)
            if file_uid:
                where.append("file_uid = %s"); params.append(file_uid)
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM action_run {clause} ORDER BY ts DESC LIMIT %s",
                            (*params, limit))
                return _rows(cur)
        finally:
            conn.close()

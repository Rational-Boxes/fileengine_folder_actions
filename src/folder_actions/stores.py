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
                        created_by: str = "") -> dict:
        conn = self._conn_t(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO action_binding "
                    "(folder_uid, recursive, action_type, on_events, config, created_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                    (folder_uid, recursive, action_type, on_events, Json(config), created_by))
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
        allowed = {"recursive", "on_events", "config", "enabled", "action_type"}
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
    def create_classifier_set(self, tenant: str, name: str, created_by: str = "") -> str:
        conn = self._conn_t(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO classifier_set (name, created_by) VALUES (%s,%s) "
                            "RETURNING id", (name, created_by))
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
                cur.execute("SELECT id, name, created_by, created_at, updated_at "
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
                cur.execute("SELECT id, name FROM classifier_set WHERE id = %s", (set_id,))
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

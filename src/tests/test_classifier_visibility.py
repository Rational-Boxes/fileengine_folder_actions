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

"""Classifier sets: readable by any tenant member, changeable by administrators only.

A folder writer who is not an administrator could choose the sorter in the binding
editor and then hit "tenant administrator required" from the dropdown that lists the
sets. These pin the split: the reads a writer needs to CHOOSE a set work for a member
and return names only; every change, the scoring test and export stay admin-only.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from folder_actions import classifier_api
from folder_actions.deps import identity, require_tenant_admin
from folder_actions.ldap_auth import Identity

FULL = {"id": "s1", "name": "Invoices", "managed_by": None,
        "classifiers": [{"id": "c1", "name": "invoice",
                         "terms": [{"term": "total due", "distance": 1, "weight": 3.0}]}]}


class _Store:
    def list_classifier_sets(self, tenant):
        return [{"id": "s1", "name": "Invoices", "created_by": "admin@x", "managed_by": None}]

    def get_classifier_set_full(self, tenant, set_id):
        return FULL if set_id == "s1" else None

    def create_classifier_set(self, *a, **k):
        raise AssertionError("a member must never reach the store's writes")


def _client(roles):
    app = FastAPI()
    app.state.store = _Store()
    app.include_router(classifier_api.router)
    ident = Identity(user="jo@x", tenant="acme", roles=roles)
    app.dependency_overrides[identity] = lambda: ident

    # require_tenant_admin resolves the caller itself, so it is overridden with the
    # SAME rule rather than left to authenticate for real.
    def _admin_only():
        if not ident.is_admin:
            raise HTTPException(status_code=403, detail="tenant administrator required")
        return ident
    app.dependency_overrides[require_tenant_admin] = _admin_only
    return TestClient(app)


def test_a_member_can_list_sets_by_name_only():
    r = _client(["users"]).get("/classifier-sets")
    assert r.status_code == 200
    assert r.json() == [{"id": "s1", "name": "Invoices"}]


def test_a_member_sees_classification_names_but_not_terms():
    r = _client(["users"]).get("/classifier-sets/s1")
    assert r.status_code == 200
    body = r.json()
    assert body == {"id": "s1", "name": "Invoices", "classifiers": [{"id": "c1", "name": "invoice"}]}
    assert "terms" not in str(body)


def test_an_administrator_still_gets_the_whole_set():
    r = _client(["users", "administrators"]).get("/classifier-sets/s1")
    assert r.json()["classifiers"][0]["terms"]


def test_an_unknown_set_is_404_for_a_member():
    assert _client(["users"]).get("/classifier-sets/nope").status_code == 404


def test_changing_a_set_is_still_administrators_only():
    c = _client(["users"])
    assert c.post("/classifier-sets", json={"name": "x"}).status_code == 403
    assert c.put("/classifier-sets/s1", json={"name": "y"}).status_code == 403
    assert c.delete("/classifier-sets/s1").status_code == 403
    assert c.get("/classifier-sets/s1/export").status_code == 403
    assert c.post("/classifier-sets/s1/test", json={"text": "total due"}).status_code == 403

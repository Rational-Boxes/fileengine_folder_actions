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

"""Classifier-set editor API (SPECIFICATIONS.md §7.3.1).

A classifier set is a reusable, **tenant-scoped** object (defined once, routed
per-folder by ``sorter_route``). Because a set spans folders it is **not** governed
by any single folder's ACL — the whole editor surface is **tenant-admin gated**
(``deps.require_tenant_admin``). The endpoints:

  GET/POST     /classifier-sets
  GET/PUT/DELETE /classifier-sets/{id}
  POST         /classifier-sets/{id}/classifiers
  POST         /classifier-sets/{id}/classifiers/{cid}/terms
  POST         /classifier-sets/import           (SmolDocBot YAML string or file upload)
  GET          /classifier-sets/{id}/export      (text/yaml)
  POST         /classifier-sets/{id}/test        {text | file_uid} -> {scores, matches}

The ``test`` endpoint additionally enforces READ on any ``file_uid`` it scores, as
the calling user (§7.3.1); scores are unbounded weighted sums, so the returned
per-term matches let an author calibrate per-folder thresholds.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import (APIRouter, Depends, File, HTTPException, Request,
                     UploadFile)
from fastapi.responses import Response
from pydantic import BaseModel

from . import classifier_io
from .classifier import document_classifier_simple, find_term_matches, normalize_text
from .config import Config
from .deps import require_tenant_admin
from .ldap_auth import Identity

log = logging.getLogger("folder_actions.classifier_api")

router = APIRouter()


# -------------------------------- sets -------------------------------------
class SetCreate(BaseModel):
    name: str


class SetUpdate(BaseModel):
    name: Optional[str] = None
    # Optional full replacement of the set's classifications+terms.
    classifiers: Optional[list[dict]] = None


class ClassifierCreate(BaseModel):
    name: str
    position: int = 0


class TermCreate(BaseModel):
    term: str
    distance: int = 0
    weight: float = 1.0


class TestRequest(BaseModel):
    text: Optional[str] = None
    file_uid: Optional[str] = None


@router.get("/classifier-sets")
def list_sets(request: Request, ident: Identity = Depends(require_tenant_admin)) -> list[dict]:
    return request.app.state.store.list_classifier_sets(ident.tenant)


@router.post("/classifier-sets")
def create_set(request: Request, body: SetCreate,
               ident: Identity = Depends(require_tenant_admin)) -> dict:
    set_id = request.app.state.store.create_classifier_set(
        ident.tenant, body.name, created_by=ident.user)
    return {"id": set_id, "name": body.name}


@router.get("/classifier-sets/{set_id}")
def get_set(set_id: str, request: Request,
            ident: Identity = Depends(require_tenant_admin)) -> dict:
    full = request.app.state.store.get_classifier_set_full(ident.tenant, set_id)
    if full is None:
        raise HTTPException(status_code=404, detail="Classifier set not found")
    return full


@router.put("/classifier-sets/{set_id}")
def update_set(set_id: str, request: Request, body: SetUpdate,
               ident: Identity = Depends(require_tenant_admin)) -> dict:
    """Replace a set's definition. The available Store primitives have no in-place
    rename/patch, so a full replace is done by rebuilding from the (optionally new)
    name + classifiers. NOTE: this mints a **new** set id — callers that reference
    the set by id (e.g. ``sorter_route.classifier_set_id``) must be re-pointed.
    A dedicated ``Store.update_classifier_set`` would let this preserve the id (TODO)."""
    store = request.app.state.store
    existing = store.get_classifier_set_full(ident.tenant, set_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Classifier set not found")

    name = body.name or existing.get("name")
    classifiers = body.classifiers if body.classifiers is not None else existing.get("classifiers", [])

    store.delete_classifier_set(ident.tenant, set_id)
    new_id = store.create_classifier_set(ident.tenant, str(name), created_by=ident.user)
    for position, c in enumerate(classifiers or []):
        cid = store.add_classifier(ident.tenant, new_id, str(c["name"]),
                                   position=c.get("position", position))
        for t in c.get("terms", []) or []:
            store.add_term(ident.tenant, cid, str(t.get("term", "")),
                           int(t.get("distance", 0) or 0),
                           float(t.get("weight", 1.0) if t.get("weight") is not None else 1.0))
    return store.get_classifier_set_full(ident.tenant, new_id)


@router.delete("/classifier-sets/{set_id}")
def delete_set(set_id: str, request: Request,
               ident: Identity = Depends(require_tenant_admin)) -> dict:
    ok = request.app.state.store.delete_classifier_set(ident.tenant, set_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Classifier set not found")
    return {"deleted": True, "id": set_id}


# ------------------------- nested classifier / term ------------------------
@router.post("/classifier-sets/{set_id}/classifiers")
def add_classifier(set_id: str, request: Request, body: ClassifierCreate,
                   ident: Identity = Depends(require_tenant_admin)) -> dict:
    store = request.app.state.store
    if store.get_classifier_set_full(ident.tenant, set_id) is None:
        raise HTTPException(status_code=404, detail="Classifier set not found")
    cid = store.add_classifier(ident.tenant, set_id, body.name, position=body.position)
    return {"id": cid, "name": body.name}


@router.post("/classifier-sets/{set_id}/classifiers/{classifier_id}/terms")
def add_term(set_id: str, classifier_id: str, request: Request, body: TermCreate,
             ident: Identity = Depends(require_tenant_admin)) -> dict:
    store = request.app.state.store
    if store.get_classifier_set_full(ident.tenant, set_id) is None:
        raise HTTPException(status_code=404, detail="Classifier set not found")
    store.add_term(ident.tenant, classifier_id, body.term, body.distance, body.weight)
    return {"ok": True, "classifier_id": classifier_id, "term": body.term}


# ------------------------------ import / export ----------------------------
@router.post("/classifier-sets/import")
async def import_set(request: Request, file: Optional[UploadFile] = File(default=None),
                     ident: Identity = Depends(require_tenant_admin)) -> dict:
    """Import a SmolDocBot ``type: classifier`` set. Accepts either a multipart file
    upload (``file``) or the raw YAML as the request body."""
    if file is not None:
        content = (await file.read()).decode("utf-8", "replace")
    else:
        content = (await request.body()).decode("utf-8", "replace")
    if not content.strip():
        raise HTTPException(status_code=400, detail="empty request body — provide YAML")
    set_id = classifier_io.import_classifier_from_yaml(
        request.app.state.store, ident.tenant, content, created_by=ident.user)
    return {"id": set_id}


@router.get("/classifier-sets/{set_id}/export")
def export_set(set_id: str, request: Request,
               ident: Identity = Depends(require_tenant_admin)) -> Response:
    text = classifier_io.export_classifier_to_yaml(
        request.app.state.store, ident.tenant, set_id)
    return Response(content=text, media_type="text/yaml")


# --------------------------------- test ------------------------------------
def _read_file_text(config: Config, ident: Identity, file_uid: str) -> str:
    """Fetch a file's extracted Markdown, READ-gated as the calling user (§7.3.1)."""
    from .core_client import client_for
    ok = False
    mf = None
    try:
        mf = client_for(ident, config)
        ok = bool(mf.check_permission(file_uid, "r", tenant=ident.tenant))
    except Exception:
        log.warning("read permission check failed for %s", file_uid, exc_info=True)
        ok = False
    finally:
        if mf is not None:
            try:
                mf.close()
            except Exception:
                pass
    if not ok:
        raise HTTPException(status_code=403, detail="READ permission required on file")

    from .csai_client import CsaiClient, TextNotReady
    try:
        return CsaiClient(config).get_text(file_uid, ident.tenant)
    except TextNotReady:
        raise HTTPException(status_code=409,
                            detail="extracted text for this file is not available yet")
    except Exception:
        raise HTTPException(status_code=502, detail="failed to fetch file text from CSAI")


@router.post("/classifier-sets/{set_id}/test")
def test_set(set_id: str, request: Request, body: TestRequest,
             ident: Identity = Depends(require_tenant_admin)) -> dict:
    config: Config = request.app.state.config
    full = request.app.state.store.get_classifier_set_full(ident.tenant, set_id)
    if full is None:
        raise HTTPException(status_code=404, detail="Classifier set not found")

    text = body.text
    if not text and body.file_uid:
        # file_uid scoring: READ-gated as the caller, text fetched from CSAI.
        text = _read_file_text(config, ident, body.file_uid)
    if not text:
        raise HTTPException(status_code=400, detail="provide 'text' or a readable 'file_uid'")

    classifications = [
        {"name": c["name"], "terms": [
            {"term": t["term"], "distance": t.get("distance", 0),
             "weight": t.get("weight", 1.0)}
            for t in c.get("terms", [])]}
        for c in full.get("classifiers", [])
    ]
    scores = document_classifier_simple(text, classifications)

    # Per-term matches — the tuning affordance: shows which terms fired (§7.3.1).
    doc_words = normalize_text(text).split()
    matches = []
    for c in classifications:
        for t in c["terms"]:
            weight = float(t.get("weight", 1.0))
            if find_term_matches(doc_words, normalize_text(t["term"]),
                                 int(t.get("distance", 0) or 0), weight) > 0:
                matches.append({"classification": c["name"], "term": t["term"],
                                "weight": weight})
    return {"scores": scores, "matches": matches}

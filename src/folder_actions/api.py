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

"""The folder_actions admin HTTP surface — one explicit router (SPECIFICATIONS.md §5/§6/§9).

  GET  /healthz                                   liveness
  GET  /readyz                                     readiness (core gRPC + Postgres + LDAP)
  GET  /poolz                                      simple pool probe
  GET  /action-types                               plug-in field-schema enumeration (§6.1)
  POST /folders/{folder_uid}/actions               create a binding (WRITE on folder, §5)
  GET  /folders/{folder_uid}/actions               list a folder's bindings (READ)
  GET  /actions/{binding_id}                        one binding (READ on its folder)
  PUT  /actions/{binding_id}                        update a binding (WRITE)
  DELETE /actions/{binding_id}                      delete a binding (WRITE)
  GET  /actions/{binding_id}/runs                   run log for a binding (READ)
  GET  /folders/{folder_uid}/runs                   run log across a folder's bindings (READ)

Binding config is **ACL-governed**: creating/editing/deleting requires the caller to
hold WRITE (or MANAGE_ACL) on the folder, checked as the calling user via core
``CheckPermission``; reading requires READ (§5/§11). Webhook secrets (``token`` /
``client_secret``) are split out, encrypted at rest and never returned (§7.4/§11).
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from . import __version__
from .config import Config
from .deps import identity
from .ldap_auth import Identity, authenticate
from .plugins import base as plugin_base

log = logging.getLogger("folder_actions.api")

router = APIRouter()

# Webhook credential fields that must never be stored in plaintext config or
# returned by the API (§7.4/§11). Held either at the top level or under ``auth``.
_SECRET_KEYS = ("token", "client_secret")


# --------------------------- readiness probes ------------------------------
def _check_core(config: Config) -> bool:
    try:
        import grpc
        channel = grpc.insecure_channel(config.grpc_address)
        try:
            grpc.channel_ready_future(channel).result(timeout=2)
            return True
        finally:
            channel.close()
    except Exception:
        return False


def _check_db(config: Config) -> bool:
    try:
        from .db import connect
        conn = connect(config, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def _check_ldap(config: Config) -> bool:
    try:
        if not config.agent_user or not config.agent_password:
            return False
        return authenticate(config, config.agent_user, config.agent_password).authenticated
    except Exception:
        return False


# ------------------------------- health ------------------------------------
@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "folder_actions", "version": __version__}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    config: Config = request.app.state.config
    # The probes block (gRPC/DB/LDAP) — run them off the event loop.
    checks = {
        "core": await run_in_threadpool(_check_core, config),
        "db": await run_in_threadpool(_check_db, config),
        "ldap": await run_in_threadpool(_check_ldap, config),
    }
    ok = all(checks.values())
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks},
                        status_code=200 if ok else 503)


@router.get("/poolz")
def poolz() -> dict:
    return {"status": "ok"}


# --------------------------- authorization helper --------------------------
def _authorize_folder(config: Config, ident: Identity, folder_uid: str, perm: str) -> None:
    """Fail-closed folder ACL check *as the caller* (§5). ``perm`` is ``"r"`` (read
    a folder's bindings) or ``"w"`` (create/edit/delete). Any error → 403."""
    from .core_client import client_for
    ok = False
    mf = None
    try:
        mf = client_for(ident, config)
        ok = bool(mf.check_permission(folder_uid, perm, tenant=ident.tenant))
    except Exception:
        log.warning("folder permission check failed for %s on %s", perm, folder_uid,
                    exc_info=True)
        ok = False
    finally:
        if mf is not None:
            try:
                mf.close()
            except Exception:
                pass
    if not ok:
        raise HTTPException(status_code=403, detail="insufficient permission on folder")


# ------------------------------ serialization ------------------------------
def _scrub_config(cfg: Any) -> Any:
    """Defence-in-depth: strip any secret fields before a config leaves the API."""
    if not isinstance(cfg, dict):
        return cfg
    cfg = copy.deepcopy(cfg)
    for container in (cfg, cfg.get("auth")):
        if isinstance(container, dict):
            for k in _SECRET_KEYS:
                container.pop(k, None)
    return cfg


def _scrub_binding(b: Optional[dict]) -> Optional[dict]:
    if b is None:
        return None
    b = dict(b)
    if "config" in b:
        b["config"] = _scrub_config(b.get("config"))
    return b


def _split_secrets(cfg: dict) -> tuple[dict, dict]:
    """Return (clean_config, secrets) — pop ``token`` / ``client_secret`` from the
    config (top level and under ``auth``) so they are stored encrypted, not inline."""
    cfg = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    secrets: dict = {}
    for container in (cfg, cfg.get("auth") if isinstance(cfg.get("auth"), dict) else None):
        if not isinstance(container, dict):
            continue
        for k in _SECRET_KEYS:
            if container.get(k) not in (None, ""):
                secrets[k] = container.pop(k)
    return cfg, secrets


def _validate_config(config: Config, action_type: str, cfg: dict):
    """Validate ``action_type`` is a registered/enabled plug-in and ``cfg`` matches its
    ConfigModel (§6.1). Returns the plug-in class. Raises 422/400 on failure."""
    reg = plugin_base.registry(config.enabled_actions or None)
    plugin = reg.get(action_type)
    if plugin is None:
        raise HTTPException(status_code=400,
                            detail=f"unknown or disabled action_type {action_type!r}")
    model = getattr(plugin, "ConfigModel", None)
    if model is not None:
        try:
            model(**(cfg or {}))
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=e.errors())
    return plugin


# ----------------------------- action types --------------------------------
@router.get("/action-types")
def action_types(request: Request, ident: Identity = Depends(identity)) -> list[dict]:
    config: Config = request.app.state.config
    reg = plugin_base.registry(config.enabled_actions or None)
    out: list[dict] = []
    for cls in reg.values():
        description = getattr(cls, "description", None) or (cls.__doc__ or "").strip()
        out.append({
            "type_name": cls.type_name,
            "label": cls.label,
            "description": description,
            "supported_events": sorted(cls.supported_events),
            "fields": [f.model_dump() for f in cls.config_fields()],
        })
    return out


# ------------------------------- bindings ----------------------------------
class BindingCreate(BaseModel):
    action_type: str
    on_events: list[str] = []
    config: dict = {}
    recursive: bool = False


class BindingUpdate(BaseModel):
    action_type: Optional[str] = None
    on_events: Optional[list[str]] = None
    config: Optional[dict] = None
    recursive: Optional[bool] = None
    enabled: Optional[bool] = None


def _persist_secrets(request: Request, tenant: str, binding_id: str, secrets: dict) -> None:
    """Encrypt and store a binding's webhook secrets; 400 if no FA_SECRET_KEY."""
    if not secrets:
        return
    from .secrets import SecretBox, SecretsDisabled
    box = SecretBox(request.app.state.config.secret_key)
    if not box.enabled:
        raise HTTPException(status_code=400,
                            detail="FA_SECRET_KEY is not configured — cannot store webhook secrets")
    try:
        ciphertext = box.encrypt(secrets)
    except SecretsDisabled:
        raise HTTPException(status_code=400,
                            detail="FA_SECRET_KEY is not configured — cannot store webhook secrets")
    request.app.state.store.put_secret(tenant, binding_id, ciphertext)


@router.post("/folders/{folder_uid}/actions")
def create_binding(folder_uid: str, request: Request, body: BindingCreate,
                   ident: Identity = Depends(identity)) -> dict:
    config: Config = request.app.state.config
    store = request.app.state.store
    _authorize_folder(config, ident, folder_uid, "w")
    _validate_config(config, body.action_type, body.config)

    # Webhook credentials are split out, encrypted and stored separately (§7.4/§11).
    clean_config, secrets = (body.config, {})
    if body.action_type == "webhook":
        clean_config, secrets = _split_secrets(body.config)

    binding = store.create_binding(
        ident.tenant, folder_uid=folder_uid, action_type=body.action_type,
        on_events=body.on_events, config=clean_config, recursive=body.recursive,
        created_by=ident.user)
    if secrets:
        _persist_secrets(request, ident.tenant, str(binding["id"]), secrets)
    return _scrub_binding(binding)


@router.get("/folders/{folder_uid}/actions")
def list_bindings(folder_uid: str, request: Request,
                  ident: Identity = Depends(identity)) -> list[dict]:
    config: Config = request.app.state.config
    store = request.app.state.store
    _authorize_folder(config, ident, folder_uid, "r")
    return [_scrub_binding(b) for b in store.list_bindings_for_folder(ident.tenant, folder_uid)]


def _load_binding(request: Request, ident: Identity, binding_id: str) -> dict:
    binding = request.app.state.store.get_binding(ident.tenant, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="binding not found")
    return binding


@router.get("/actions/{binding_id}")
def get_binding(binding_id: str, request: Request,
                ident: Identity = Depends(identity)) -> dict:
    config: Config = request.app.state.config
    binding = _load_binding(request, ident, binding_id)
    _authorize_folder(config, ident, str(binding["folder_uid"]), "r")
    return _scrub_binding(binding)


@router.put("/actions/{binding_id}")
def update_binding(binding_id: str, request: Request, body: BindingUpdate,
                   ident: Identity = Depends(identity)) -> dict:
    config: Config = request.app.state.config
    store = request.app.state.store
    binding = _load_binding(request, ident, binding_id)
    _authorize_folder(config, ident, str(binding["folder_uid"]), "w")

    action_type = body.action_type or binding["action_type"]
    fields: dict = {}
    if body.action_type is not None:
        fields["action_type"] = body.action_type
    if body.on_events is not None:
        fields["on_events"] = body.on_events
    if body.recursive is not None:
        fields["recursive"] = body.recursive
    if body.enabled is not None:
        fields["enabled"] = body.enabled

    secrets: dict = {}
    if body.config is not None:
        _validate_config(config, action_type, body.config)
        clean_config, secrets = (body.config, {})
        if action_type == "webhook":
            clean_config, secrets = _split_secrets(body.config)
        fields["config"] = clean_config

    updated = store.update_binding(ident.tenant, binding_id, **fields)
    if secrets:
        _persist_secrets(request, ident.tenant, binding_id, secrets)
    return _scrub_binding(updated)


@router.delete("/actions/{binding_id}")
def delete_binding(binding_id: str, request: Request,
                   ident: Identity = Depends(identity)) -> dict:
    config: Config = request.app.state.config
    store = request.app.state.store
    binding = _load_binding(request, ident, binding_id)
    _authorize_folder(config, ident, str(binding["folder_uid"]), "w")
    store.delete_binding(ident.tenant, binding_id)
    return {"deleted": True, "id": binding_id}


# -------------------------------- run log ----------------------------------
@router.get("/actions/{binding_id}/runs")
def binding_runs(binding_id: str, request: Request, limit: int = 100,
                 ident: Identity = Depends(identity)) -> list[dict]:
    config: Config = request.app.state.config
    store = request.app.state.store
    binding = _load_binding(request, ident, binding_id)
    _authorize_folder(config, ident, str(binding["folder_uid"]), "r")
    return store.list_runs(ident.tenant, binding_id=binding_id, limit=limit)


@router.get("/folders/{folder_uid}/runs")
def folder_runs(folder_uid: str, request: Request, limit: int = 100,
                ident: Identity = Depends(identity)) -> list[dict]:
    config: Config = request.app.state.config
    store = request.app.state.store
    _authorize_folder(config, ident, folder_uid, "r")
    # No folder filter on action_run — aggregate across the folder's bindings.
    runs: list[dict] = []
    for b in store.list_bindings_for_folder(ident.tenant, folder_uid):
        runs.extend(store.list_runs(ident.tenant, binding_id=str(b["id"]), limit=limit))
    runs.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return runs[:limit]

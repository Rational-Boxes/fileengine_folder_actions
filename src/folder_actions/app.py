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

"""FastAPI application factory for the folder_actions service (SPECIFICATIONS.md §9).

The HTTP admin surface lives in ``api.py`` (bindings CRUD, ``/action-types``, run
log, health) and ``classifier_api.py`` (the classifier-set editor); ``build_app``
wires the shared services onto ``app.state`` and includes both routers:

  state.config            Config
  state.store             Store (per-tenant Postgres)
  state.token_store       TokenStore (bearer tokens)
  state.bridge_verifier   BridgeTokenVerifier (accept http_bridge tokens)

``build_app`` stays pure (no .env side effects) so tests are hermetic; ``create_app``
loads ``./.env`` first for real launches. Action plug-ins are discovered at startup:
the built-ins register on ``import folder_actions.plugins`` and third-party plug-ins
via the ``folder_actions.actions`` entry-point group.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from . import __version__
from .bridge_auth import BridgeTokenVerifier
from .config import Config
from .stores import Store
from .token_store import TokenStore

log = logging.getLogger("folder_actions.app")


def build_app(config: Config | None = None, *, store: Store | None = None,
              token_store: TokenStore | None = None,
              bridge_verifier: BridgeTokenVerifier | None = None) -> FastAPI:
    config = config or Config()
    app = FastAPI(title="folder_actions", version=__version__)

    # Capture the caller's IP into a request-scoped contextvar so per-user core
    # calls forward it (core audit source_addr). Trusted-proxy aware (§11): honors
    # FILEENGINE_TRUSTED_PROXIES exactly like the C++ bridges.
    from .core_client import request_source_addr
    from .netutil import client_ip_from_request

    @app.middleware("http")
    async def _capture_client_ip(request, call_next):
        token = request_source_addr.set(client_ip_from_request(request))
        try:
            return await call_next(request)
        finally:
            request_source_addr.reset(token)

    # Route-scoped IP allowlist for the unauthenticated monitoring endpoints
    # (security review L2). Endpoints already bind loopback; when
    # FILEENGINE_MONITORING_ALLOW_IPS is set (comma-separated client IPs), a
    # monitoring request from a non-listed address is refused with 403.
    import os as _os
    from fastapi.responses import JSONResponse as _JSONResponse
    _monitor_allow = {ip.strip() for ip in
                      _os.environ.get("FILEENGINE_MONITORING_ALLOW_IPS", "").split(",") if ip.strip()}

    @app.middleware("http")
    async def _guard_monitoring(request, call_next):
        if _monitor_allow and request.url.path in {"/healthz", "/readyz", "/poolz", "/metrics"}:
            client = request.client.host if request.client else ""
            if client not in _monitor_allow:
                return _JSONResponse({"error": "forbidden"}, status_code=403)
        return await call_next(request)

    # Browser CORS for a SPA on another origin (off unless FA_CORS_ORIGINS set).
    # Explicit origins (never "*") so credentialed bearer + X-Tenant requests work.
    if config.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.state.config = config
    app.state.store = store or Store(config)
    app.state.token_store = token_store or TokenStore(ttl_seconds=config.token_ttl)
    app.state.bridge_verifier = bridge_verifier or BridgeTokenVerifier(
        config.bridge_url, config.bridge_introspect_ttl, jwt_secret=config.jwt_secret)

    # Discover action plug-ins: importing the package registers the built-ins
    # (move_review / notify / sorter / webhook), then pick up any third-party
    # plug-ins declared on the ``folder_actions.actions`` entry-point group (§6).
    from . import plugins  # noqa: F401  (import side effect: built-ins self-register)
    from .plugins import base as plugin_base
    plugin_base.load_entrypoint_plugins()

    from .api import router as api_router
    from .classifier_api import router as classifier_router
    from .notify_templates_api import router as notify_templates_router
    app.include_router(api_router)
    app.include_router(classifier_router)
    app.include_router(notify_templates_router)
    # Prometheus scrape endpoint, guarded by the same allowlist as the other
    # monitoring routes. Reports process and per-thread state so a stuck or
    # leaking service is visible to the same scraper that watches the core.
    from . import metrics as _fe_metrics
    _fe_metrics.install(app, "folder_actions", [], {"version": str(__version__)})

    return app


def create_app() -> FastAPI:
    """ASGI factory that loads ``./.env`` then builds the app — for launching via
    ``uvicorn folder_actions.app:create_app --factory`` or the ``folder-actions`` script."""
    from .config import load_dotenv
    load_dotenv()
    return build_app(Config())


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    app = create_app()
    cfg = app.state.config
    log.info("folder_actions %s — http=%s:%s core=%s", __version__, cfg.http_host,
             cfg.http_port, cfg.grpc_address)
    uvicorn.run(app, host=cfg.http_host, port=cfg.http_port)


if __name__ == "__main__":
    main()

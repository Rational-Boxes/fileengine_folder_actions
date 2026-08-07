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

"""Webhook action (SPECIFICATIONS.md §7.4).

POSTs a JSON payload describing the event to a remote URL, with an optional MIME-type
firing whitelist (§7.4.1, content-sniffed / fail-closed), admin-authored static
``context`` (§7.4.2), static-bearer or OAuth2 client-credentials auth (secrets from
the encrypted secret box, never logged), bounded exponential-backoff retries, and a
``move_to`` / ``metadata`` response contract. stdlib ``urllib`` only (no httpx).

Scoped read-back token minting (``grant_read``, §7.4) is out of scope for v1 — see the
TODO in ``_build_payload``; the flag is accepted and echoed but no token is minted yet."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from pydantic import BaseModel

from .base import ActionContext, ActionResult, FieldDescriptor, FieldOption, register

log = logging.getLogger("folder_actions.plugins.webhook")

# The recognized event types a webhook may bind to (SPECIFICATIONS.md §3.1).
SUPPORTED = frozenset({
    "file.created", "file.updated", "file.moved", "file.renamed", "file.deleted",
    "file.restored", "review.approved", "review.rejected", "thread.opened",
    "comment.created", "mention.created", "thread.resolved", "conversion.complete",
    "conversion.failed",
})

_BACKOFF_CAP_S = 30.0


@register
class WebhookAction:
    type_name = "webhook"
    label = "Webhook call"
    supported_events = SUPPORTED

    # Process-lifetime OAuth2 client-credentials token cache: key -> (token, expiry_ts).
    _oauth_cache: dict[str, tuple[str, float]] = {}

    class ConfigModel(BaseModel):
        url: str
        auth: dict = {}                 # {type: bearer|oauth2_client_credentials, ...}
        context: dict[str, str] = {}    # admin-authored static context (§7.4.2)
        grant_read: bool = False        # mint scoped read-back token (v1: accepted, TODO)
        timeout_s: int = 10
        max_retries: int = 5

    @classmethod
    def config_fields(cls) -> list[FieldDescriptor]:
        return [
            FieldDescriptor(key="url", label="URL", type="string", required=True,
                            pattern="^https?://", max_length=2048,
                            help="The endpoint the event is POSTed to."),
            FieldDescriptor(
                key="auth_type", label="Auth", type="select",
                default="bearer",
                options=[
                    FieldOption(value="bearer", label="Bearer token"),
                    FieldOption(value="oauth2_client_credentials",
                                label="OAuth2 client credentials"),
                ],
                help="How to authenticate to the remote endpoint."),
            FieldDescriptor(key="token", label="Bearer token", type="secret",
                            secret=True,
                            visible_when={"key": "auth_type", "equals": "bearer"}),
            FieldDescriptor(key="token_url", label="Token URL", type="string",
                            pattern="^https?://", max_length=2048,
                            visible_when={"key": "auth_type",
                                          "equals": "oauth2_client_credentials"}),
            FieldDescriptor(key="client_id", label="Client ID", type="string",
                            visible_when={"key": "auth_type",
                                          "equals": "oauth2_client_credentials"}),
            FieldDescriptor(key="client_secret", label="Client secret", type="secret",
                            secret=True,
                            visible_when={"key": "auth_type",
                                          "equals": "oauth2_client_credentials"}),
            FieldDescriptor(
                key="context", label="Custom context", type="group",
                help="Static key:values sent verbatim under the payload 'context' key.",
                item_fields=[
                    FieldDescriptor(key="key", label="Key", type="string", required=True),
                    FieldDescriptor(key="value", label="Value", type="string"),
                ]),
            FieldDescriptor(key="grant_read", label="Grant scoped read-back",
                            type="boolean", default=False,
                            help="Include a short-lived READ token so the remote can "
                                 "fetch the file (v1: not yet minted)."),
            FieldDescriptor(key="timeout_s", label="Timeout (seconds)", type="integer",
                            default=10, min=1, max=120, step=1),
            FieldDescriptor(key="max_retries", label="Max retries", type="integer",
                            default=5, min=0, max=10, step=1),
        ]

    # ------------------------------------------------------------------ execute
    def execute(self, event: dict, config: "WebhookAction.ConfigModel",
                ctx: ActionContext) -> ActionResult:
        # Which events fire this webhook is the binding's on_events (enforced by the
        # consumer) — no redundant per-action event list.
        etype = event.get("type")
        if not config.url:
            return ActionResult.failed("no_url")

        file_uid = event.get("file_uid") or ""

        # (a) Resolve the file's content-sniffed MIME for the payload (best-effort).
        # The MIME *whitelist* is now a binding-level filter enforced by the consumer
        # for any action (§7.4.1 generalized) — not re-implemented here.
        mime: Any = None
        if file_uid:
            try:
                mime = ctx.mime.resolve(file_uid)
            except Exception:
                mime = None

        # (b) Decrypt stored secrets (token / client_secret) — never logged.
        secret = self._load_secret(ctx)
        auth = dict(config.auth or {})

        # (c) Build the request payload (§7.4).
        payload = self._build_payload(event, config, ctx, mime)
        body = json.dumps(payload).encode("utf-8")

        # (d) Auth header.
        headers = {"Content-Type": "application/json"}
        try:
            auth_header = self._auth_header(auth, secret, config)
        except _AuthError as e:
            return ActionResult.failed("auth_error", detail=str(e))
        if auth_header:
            headers["Authorization"] = auth_header

        # (e) POST with bounded exponential-backoff retries.
        status, resp_body, transient = self._post_with_retries(
            config.url, body, headers, config.timeout_s, config.max_retries, ctx)

        if status is not None and 200 <= status < 300:
            return self._handle_2xx(status, resp_body, event, ctx)

        # 4xx (except 429) is caller misconfiguration — no retry.
        if not transient:
            return ActionResult.failed("http_error", status=status)
        # Transient exhausted — leave retryable so the consumer may redeliver.
        return ActionResult.failed("exhausted", retryable=True, status=status)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _load_secret(ctx: ActionContext) -> dict:
        try:
            blob = ctx.store.get_secret(ctx.tenant, ctx.binding_id)
        except Exception:
            return {}
        if not blob:
            return {}
        try:
            obj = ctx.secrets.decrypt(blob)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            ctx.log.warning("webhook: secret decrypt failed for binding %s",
                            ctx.binding_id)
            return {}

    def _build_payload(self, event: dict, config: "WebhookAction.ConfigModel",
                       ctx: ActionContext, mime: Any) -> dict:
        file_uid = event.get("file_uid") or ""
        # TODO(§7.4): scoped read-back token minting is out of scope for v1. When
        # config.grant_read is set, mint a short-lived, single-file READ-scoped token
        # (via http_bridge OAuth/introspection or a time-boxed core ACL grant) and add
        # it here so the remote can fetch original/rendition bytes, then revoke it.
        payload = {
            "event": event.get("type"),
            "document_id": file_uid,
            "version": event.get("version", ""),
            "tenant": event.get("tenant", ctx.tenant),
            "metadata": ctx.core.metadata(file_uid) if file_uid else {},
            "mime": mime,
            "conversion": self._conversion_block(event),
            "user": {"actor": event.get("actor")},
            "folder_uid": ctx.folder_uid,
            "context": dict(config.context or {}),
        }
        if config.grant_read:
            payload["grant_read"] = True  # remote hint; token not yet minted (see TODO)
        return payload

    @staticmethod
    def _conversion_block(event: dict) -> dict:
        etype = event.get("type")
        renditions = event.get("renditions") or []
        if etype == "conversion.complete":
            return {"status": "complete", "renditions": renditions}
        if etype == "conversion.failed":
            return {"status": "failed", "reason": event.get("reason"),
                    "renditions": renditions}
        # Not gated on conversion — best-effort from any rendition info present.
        return {"status": "unknown", "renditions": renditions}

    def _auth_header(self, auth: dict, secret: dict,
                     config: "WebhookAction.ConfigModel") -> str:
        auth_type = (auth.get("type") or "bearer").strip()
        if auth_type == "bearer":
            token = secret.get("token") or auth.get("token")
            return f"Bearer {token}" if token else ""
        if auth_type == "oauth2_client_credentials":
            token = self._oauth_token(auth, secret, config)
            return f"Bearer {token}" if token else ""
        raise _AuthError(f"unknown auth type {auth_type!r}")

    def _oauth_token(self, auth: dict, secret: dict,
                     config: "WebhookAction.ConfigModel") -> str:
        token_url = auth.get("token_url")
        client_id = auth.get("client_id")
        client_secret = secret.get("client_secret") or auth.get("client_secret")
        if not token_url or not client_id:
            raise _AuthError("oauth2 requires token_url and client_id")

        cache_key = f"{token_url}|{client_id}"
        cached = self._oauth_cache.get(cache_key)
        now = time.time()
        if cached and cached[1] > now + 5:
            return cached[0]

        form = {"grant_type": "client_credentials", "client_id": client_id}
        if client_secret:
            form["client_secret"] = client_secret
        scopes = auth.get("scopes")
        if scopes:
            form["scope"] = " ".join(scopes) if isinstance(scopes, list) else str(scopes)
        data = urllib.parse.urlencode(form).encode("utf-8")
        req = urllib.request.Request(
            token_url, data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=config.timeout_s) as resp:
                tok = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # do not leak client_secret into the message
            raise _AuthError(f"token fetch failed: {type(e).__name__}")
        access = tok.get("access_token")
        if not access:
            raise _AuthError("token response missing access_token")
        expires_in = tok.get("expires_in")
        try:
            ttl = float(expires_in) if expires_in is not None else 300.0
        except (TypeError, ValueError):
            ttl = 300.0
        self._oauth_cache[cache_key] = (access, now + ttl)
        return access

    @staticmethod
    def _post(url: str, data: bytes, headers: dict, timeout: int):
        """POST; returns (status, body). Raises on connection/timeout errors."""
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, (e.read() if e.fp else b"")

    def _post_with_retries(self, url: str, body: bytes, headers: dict, timeout: int,
                           max_retries: int, ctx: ActionContext):
        """Returns (status|None, body, transient). ``transient`` marks a retryable
        class of failure that was exhausted (connection/timeout/5xx/429)."""
        attempt = 0
        while True:
            status = None
            resp_body = b""
            transient = False
            try:
                status, resp_body = self._post(url, body, headers, timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                transient = True
            else:
                if 200 <= status < 300:
                    return status, resp_body, False
                # 429 and 5xx are retryable; other 4xx are terminal.
                transient = status == 429 or 500 <= status < 600
                if not transient:
                    return status, resp_body, False

            if attempt >= max_retries:
                ctx.log.warning("webhook: exhausted retries for %s (last status %s)",
                                url, status)
                return status, resp_body, True
            delay = min(_BACKOFF_CAP_S, 2.0 ** attempt)
            time.sleep(delay)
            attempt += 1

    def _handle_2xx(self, status: int, resp_body: bytes, event: dict,
                    ctx: ActionContext) -> ActionResult:
        file_uid = event.get("file_uid") or ""
        detail: dict[str, Any] = {"status": status}
        try:
            data = json.loads(resp_body.decode("utf-8")) if resp_body else {}
        except (ValueError, UnicodeDecodeError):
            data = {}
        if isinstance(data, dict) and file_uid:
            move_to = data.get("move_to")
            if move_to:
                if ctx.core.move(file_uid, move_to):
                    detail["moved_to"] = move_to
            meta = data.get("metadata")
            if isinstance(meta, dict):
                applied = []
                for k, v in meta.items():
                    if ctx.core.set_metadata(file_uid, k, v):
                        applied.append(k)
                if applied:
                    detail["metadata_keys"] = applied
        return ActionResult.done(**detail)


class _AuthError(Exception):
    """Auth could not be established (bad config / token fetch failure)."""

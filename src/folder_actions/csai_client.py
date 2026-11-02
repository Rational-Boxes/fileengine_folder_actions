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

"""HTTP client to convert_search_ai for the sorter (SPECIFICATIONS.md §7.3).

Fetches a file's extracted **Markdown** (the search-index text — the canonical
normalized-text surface the classifier consumes) and can request (re)conversion.
Authenticates to CSAI with the service principal's LDAP credentials via HTTP Basic
(CSAI binds and enforces READ as that identity). stdlib-only (no httpx runtime dep)."""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("folder_actions.csai")


class TextNotReady(Exception):
    """CSAI has no extracted text for the file yet (202 / not found)."""


class CsaiError(Exception):
    """A csai call failed. ``retryable`` for 429 / 5xx / unreachable."""

    def __init__(self, status: int, body: bytes = b""):
        self.status = status
        try:
            self.body = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            self.body = {}
        self.kind = (self.body.get("kind") if isinstance(self.body, dict) else "") or ""
        self.retryable = status == 0 or status == 429 or status >= 500
        super().__init__(f"csai HTTP {status} {self.kind}".strip())


class CsaiClient:
    def __init__(self, config):
        self.base = (config.csai_base_url or "").rstrip("/")
        self.timeout = config.csai_timeout_s
        self._auth = None
        user, pw = config.agent_user, config.agent_password
        if user:
            raw = f"{user}:{pw}".encode("utf-8")
            self._auth = "Basic " + base64.b64encode(raw).decode("ascii")

    def _request(self, method: str, path: str, tenant: str,
                 body: dict | None = None) -> tuple[int, bytes]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(f"{self.base}{path}", method=method, data=data)
        if self._auth:
            req.add_header("Authorization", self._auth)
        if tenant:
            req.add_header("X-Tenant", tenant)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read() if e.fp else b""

    def _json(self, method: str, path: str, tenant: str, body: dict | None = None) -> dict:
        try:
            status, raw = self._request(method, path, tenant, body)
        except (urllib.error.URLError, OSError) as e:
            log.warning("csai unreachable: %s", e)
            raise CsaiError(0)
        if 200 <= status < 300:
            return json.loads(raw.decode("utf-8")) if raw else {}
        raise CsaiError(status, raw)

    # -- extraction (convert_search_ai EXTRACTION_PROMPT_SETS.md §6) --
    def start_extract_job(self, tenant: str, *, file_uid: str, version: str, prompt_set: str,
                          revision: int | None = None, markup: bool = True,
                          binding_id: str = "") -> str:
        """Queue an extraction; csai answers at once and announces the outcome
        as extraction.complete / extraction.failed. Returns the job id."""
        out = self._json("POST", "/v1/extract/jobs", tenant, {
            "file_uid": file_uid, "version": version or "", "prompt_set": prompt_set,
            "revision": revision, "markup": bool(markup), "binding_id": binding_id})
        return out.get("job_id", "")

    def get_extractions(self, tenant: str, file_uid: str, *, version: str = "",
                        entry: str = "", prompt_set: str = "") -> list:
        """A document version's extracted values with what they mean (field
        definitions or the raw schema), read as the service principal."""
        params = urllib.parse.urlencode({k: v for k, v in (("version", version), ("entry", entry),
                                                           ("prompt_set", prompt_set)) if v})
        out = self._json("GET", f"/v1/documents/{urllib.parse.quote(file_uid)}/extractions"
                                f"{'?' + params if params else ''}", tenant)
        return out.get("entries") or []

    def list_prompt_sets(self, tenant: str) -> list:
        return self._json("GET", "/v1/prompt-sets?limit=500", tenant).get("prompt_sets") or []

    def get_text(self, file_uid: str, tenant: str) -> str:
        """The file's extracted Markdown. Raises TextNotReady when unavailable."""
        status, body = self._request("GET", f"/documents/{file_uid}/text", tenant)
        if status == 200:
            try:
                data = json.loads(body.decode("utf-8"))
            except ValueError:
                return body.decode("utf-8", "replace")
            if isinstance(data, dict):
                return data.get("text") or data.get("markdown") or ""
            return str(data)
        if status in (202, 404):
            raise TextNotReady(f"CSAI text for {file_uid} not ready (HTTP {status})")
        raise RuntimeError(f"CSAI text fetch failed for {file_uid}: HTTP {status}")

    def request_convert(self, file_uid: str, tenant: str) -> bool:
        """Ask CSAI to (re)generate renditions/text. Best-effort; returns success."""
        status, _ = self._request("POST", f"/documents/{file_uid}/convert", tenant)
        if status not in (200, 201, 202):
            log.warning("CSAI convert request for %s returned HTTP %s", file_uid, status)
        return status in (200, 201, 202)

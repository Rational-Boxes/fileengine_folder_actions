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

"""HTTP client to the discussion service for the raise_review action (SPEC §7.x).

Raises a review request on a file, assigned to reviewers, via the discussion
service's ``POST /files/{file_uid}/reviews``. Authenticates as the folder_actions
service principal over HTTP Basic (LDAP creds) — the requester is the service
account. stdlib-only (no httpx runtime dep)."""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("folder_actions.discussion")


class DiscussionClient:
    def __init__(self, config):
        self.base = (config.discuss_base_url or "").rstrip("/")
        self.timeout = config.discuss_timeout_s
        self._auth = None
        user, pw = config.agent_user, config.agent_password
        if user:
            raw = f"{user}:{pw}".encode("utf-8")
            self._auth = "Basic " + base64.b64encode(raw).decode("ascii")

    def raise_review(self, file_uid: str, reviewers: list[str], tenant: str,
                     version: str = "", thread_id: str | None = None) -> tuple[int, dict]:
        """POST a review request. Returns (status, body). The discussion service
        validates each reviewer holds READ on the file (422 lists any who don't)."""
        payload: dict = {"reviewers": reviewers}
        if version:
            payload["version"] = version
        if thread_id:
            payload["thread_id"] = thread_id
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/files/{file_uid}/reviews", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self._auth:
            req.add_header("Authorization", self._auth)
        if tenant:
            req.add_header("X-Tenant", tenant)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, _json(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, _json(e.read() if e.fp else b"")
        except Exception as e:  # transport error — let the caller mark retryable
            log.warning("discussion raise_review transport error for %s: %s", file_uid, e)
            return 0, {"error": str(e)}


def _json(raw: bytes) -> dict:
    try:
        d = json.loads(raw.decode("utf-8"))
        return d if isinstance(d, dict) else {"data": d}
    except Exception:
        return {}

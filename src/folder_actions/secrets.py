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

"""Encryption at rest for webhook credentials (SPECIFICATIONS.md §10/§11).

Secrets (webhook bearer token / OAuth client secret) are stored ciphertext-only and
never returned by the admin API. Uses Fernet (AES-128-CBC + HMAC) keyed by
``FA_SECRET_KEY``. If no key is configured, storing a secret raises — the service
refuses to persist credentials it cannot protect."""
from __future__ import annotations

import json
from typing import Any, Optional


class SecretsDisabled(RuntimeError):
    """Raised when a webhook secret must be stored but FA_SECRET_KEY is unset."""


class SecretBox:
    def __init__(self, key: str):
        self._key = (key or "").strip()
        self._fernet = None

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    def _cipher(self):
        if self._fernet is None:
            if not self._key:
                raise SecretsDisabled(
                    "FA_SECRET_KEY is not set — cannot store/read webhook secrets")
            from cryptography.fernet import Fernet
            self._fernet = Fernet(self._key.encode("utf-8"))
        return self._fernet

    def encrypt(self, obj: Any) -> bytes:
        """Encrypt a JSON-serialisable object (e.g. the secret fields of a webhook)."""
        return self._cipher().encrypt(json.dumps(obj).encode("utf-8"))

    def decrypt(self, blob: bytes) -> Any:
        if not blob:
            return {}
        return json.loads(self._cipher().decrypt(bytes(blob)).decode("utf-8"))

    @staticmethod
    def generate_key() -> str:
        """A fresh urlsafe key for FA_SECRET_KEY (ops convenience)."""
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode("utf-8")

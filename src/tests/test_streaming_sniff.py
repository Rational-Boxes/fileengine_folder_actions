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

"""MIME sniffing must not pull the whole file.

`read_prefix` used to be `get(uid).read(n)`, which transfers and buffers the
ENTIRE file before discarding all but the first few kilobytes. Sniffing a 2 GiB
upload cost 2 GiB of wire and heap to look at 8 KB — and it runs on every
webhook firing.
"""
from __future__ import annotations

import pytest

from folder_actions.core_client import CoreClient


class _Client:
    """Stands in for ManagedFiles, counting what actually gets pulled."""

    def __init__(self, chunk=b"x" * 1024, count=1_000_000):
        self.chunk, self.count = chunk, count
        self.pulled = 0
        self.closed = False
        self.get_called = False

    def get_stream(self, uid, tenant=None, **kw):
        client = self

        class _Stream:
            def __iter__(self):
                for _ in range(client.count):
                    client.pulled += 1
                    yield client.chunk

            def close(self):
                client.closed = True

        return _Stream()

    def get(self, uid, **kw):
        # The old path. If anything reaches for it, the test should say so.
        self.get_called = True
        raise AssertionError("read_prefix must not use the buffering get()")


@pytest.fixture
def core(monkeypatch):
    c = CoreClient.__new__(CoreClient)
    c.tenant = "default"
    stub = _Client()
    monkeypatch.setattr(c, "_client", lambda: stub, raising=False)
    return c, stub


def test_sniffing_reads_one_chunk_not_the_whole_file(core):
    c, stub = core
    data = c.read_prefix("uid", 8192)
    assert len(data) == 8192
    # 1 KiB chunks for an 8 KiB prefix: 8 pulls, not a million.
    assert stub.pulled <= 9, f"pulled {stub.pulled} chunks for an 8 KiB prefix"
    assert stub.get_called is False


def test_the_abandoned_stream_is_closed(core):
    c, stub = core
    c.read_prefix("uid", 4096)
    assert stub.closed is True, "an abandoned stream must be closed, not leaked"


def test_a_file_shorter_than_the_prefix_is_returned_whole(monkeypatch):
    c = CoreClient.__new__(CoreClient)
    c.tenant = "default"
    stub = _Client(chunk=b"%PDF-1.7", count=1)
    monkeypatch.setattr(c, "_client", lambda: stub, raising=False)
    assert c.read_prefix("uid", 8192) == b"%PDF-1.7"


def test_the_prefix_is_never_longer_than_asked_for(monkeypatch):
    c = CoreClient.__new__(CoreClient)
    c.tenant = "default"
    stub = _Client(chunk=b"y" * 100_000, count=5)
    monkeypatch.setattr(c, "_client", lambda: stub, raising=False)
    assert len(c.read_prefix("uid", 8192)) == 8192

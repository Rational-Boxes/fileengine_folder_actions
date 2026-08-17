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

"""The reconcile idempotency guard at the dispatch boundary (SPECIFICATIONS.md §8).

Covers the double-run defect: a core ``file.created`` event records ``version=''``
while the sweep stamps the version read back from ``stat``, so a guard keyed on
version equality alone let every newly-created file run twice."""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import BaseModel

from folder_actions.consumer import EventConsumer
from folder_actions.plugins.base import ActionResult, _REGISTRY

NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


class Spy:
    """A registered plug-in that records every execution."""
    type_name = "spy"
    label = "Spy"
    supported_events = frozenset({"file.created"})
    calls: list = []

    class ConfigModel(BaseModel):
        pass

    @classmethod
    def config_fields(cls):
        return []

    def execute(self, event, config, ctx):
        Spy.calls.append(event.get("file_uid"))
        return ActionResult()


@pytest.fixture(autouse=True)
def _register_spy():
    Spy.calls = []
    _REGISTRY["spy"] = Spy
    yield
    _REGISTRY.pop("spy", None)


class FakeConfig:
    enabled_actions: set = set()
    agent_user = "svc@example.com"
    tenant = "default"


class FakeStore:
    """Records runs; answers the two idempotency questions from that record."""
    def __init__(self, runs=None):
        # each run: (binding_id, file_uid, version, ts)
        self.runs = list(runs or [])
        self.recorded = []

    def run_exists(self, tenant, event_id, binding_id):
        return False

    def run_covers_file(self, tenant, binding_id, file_uid, version, since=None):
        for b, f, v, ts in self.runs:
            if b != binding_id or f != file_uid:
                continue
            if v == version:
                return True
            if since is not None and ts >= since:
                return True
        return False

    def record_run(self, tenant, **kw):
        self.recorded.append(kw)

    def list_enabled_bindings(self, tenant):
        return [_binding()]


def _binding():
    return {"id": "b1", "folder_uid": "F", "recursive": False, "enabled": True,
            "on_events": ["file.created"], "action_type": "spy", "config": {},
            "mime_types": []}


class FakeCore:
    def parent_of(self, uid):
        return "F"

    def stat(self, uid):
        raise AssertionError("not needed")


def _consumer(store):
    return EventConsumer(FakeConfig(), store, core=FakeCore(), csai=object(),
                         directory=object(), mailer=object(), secrets=object(),
                         mime=object(), discussion=object())


def _reconcile_event(version="v2", since=NOW):
    return {"type": "file.created", "event_id": "reconcile:file.created:abc",
            "tenant": "default", "file_uid": "x", "parent_uid": "F",
            "version": version, "reconciled": True, "reconciled_since": since}


def test_runs_when_nothing_covers_the_file():
    store = FakeStore()
    _consumer(store).handle(_reconcile_event(), collapse_on_content=True)
    assert Spy.calls == ["x"]


def test_skips_when_a_run_exists_at_the_same_version():
    store = FakeStore(runs=[("b1", "x", "v2", NOW - timedelta(days=1))])
    _consumer(store).handle(_reconcile_event(version="v2"), collapse_on_content=True)
    assert Spy.calls == []


def test_skips_when_the_live_consumer_ran_with_an_empty_version():
    # THE regression: core's file.created carries version=''; the sweep stamps 'v2'.
    # Version equality misses it, so the time bound must catch it.
    store = FakeStore(runs=[("b1", "x", "", NOW + timedelta(seconds=5))])
    _consumer(store).handle(_reconcile_event(version="v2", since=NOW),
                            collapse_on_content=True)
    assert Spy.calls == []


def test_runs_again_when_the_file_changed_after_the_last_run():
    # A run that predates the modification covered older content, not this one.
    store = FakeStore(runs=[("b1", "x", "v1", NOW - timedelta(hours=2))])
    _consumer(store).handle(_reconcile_event(version="v2", since=NOW),
                            collapse_on_content=True)
    assert Spy.calls == ["x"]


def test_another_bindings_run_does_not_cover_this_binding():
    store = FakeStore(runs=[("b2", "x", "v2", NOW + timedelta(seconds=5))])
    _consumer(store).handle(_reconcile_event(version="v2"), collapse_on_content=True)
    assert Spy.calls == ["x"]


def test_live_dispatch_is_unaffected_by_the_guard():
    # Without collapse_on_content the live consumer must not consult it at all —
    # every core event runs on its own (event_id, binding_id) dedupe.
    store = FakeStore(runs=[("b1", "x", "v2", NOW + timedelta(seconds=5))])
    ev = {"type": "file.created", "event_id": "core-uuid", "tenant": "default",
          "file_uid": "x", "parent_uid": "F", "version": "v2"}
    _consumer(store).handle(ev)
    assert Spy.calls == ["x"]
